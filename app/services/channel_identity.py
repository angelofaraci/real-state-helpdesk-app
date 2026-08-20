"""Inbound-message sender identity resolution + auto-provisioning (stage 5
— multichannel: email + WhatsApp).

An inbound webhook (email via Mailgun, WhatsApp via the Meta Cloud API)
arrives with only a raw sender address/phone number, never an
authenticated principal. Resolving WHICH `User` (and hence which
organization) that sender is happens here, BEFORE any ticket/message is
ever written:

- A sender already known in the SAME organization the inbound channel
  resolved to (via its `whatsapp_phone_number_id` / `support_email_address`
  routing, see `OrganizationRepository`) is returned as-is.
- An unknown sender is auto-provisioned as a `PENDING`, password-less
  `TENANT` — never able to log in until a real invite/reset flow upgrades
  it, but immediately usable to own tickets/messages.
- A sender already known in a DIFFERENT organization is a hard stop
  (isolation invariant I4): `CrossOrgSenderConflict` is raised and NOTHING
  is created. The caller (a webhook route, added in a later PR) is
  responsible for logging that at WARNING with
  `{organization_id, existing_user_organization_id, channel}` — never the
  address itself — and acking the webhook as a no-op.

The lookups below are deliberately raw `select(User)` statements, NOT
`UserRepository` (a `ScopedRepository`, structurally closed to unscoped
queries by its own `__init_subclass__` safety net — see
`app.repositories.base`). That is correct here, not a bypass: resolving
which org a sender belongs to IS the scope-resolution step itself, so
there is no `OrgScope` yet to run a scoped query under. Same rationale/
precedent as `app.workers.classification.sweep_pending_classifications`'s
direct `Ticket` query.
"""

from __future__ import annotations

import re
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TicketChannel, UserRole, UserStatus
from app.models.user import User

# E.164: a leading `+`, a nonzero first digit, then 6-14 more digits (7-15
# digits total after the `+`) — mirrors `ck_users_phone_number_e164`
# (migration 0007) exactly.
_MSISDN_PATTERN = re.compile(r"^\+[1-9][0-9]{6,14}$")
_MSISDN_STRIP_CHARS = re.compile(r"[\s\-()]")


class CrossOrgSenderConflict(Exception):
    """Raised when an inbound message's sender (email or phone number)
    already belongs to a `User` in a DIFFERENT organization than the one
    resolved for this inbound channel. No user is ever created for this
    request (isolation invariant I4)."""

    def __init__(
        self,
        *,
        organization_id: UUID,
        existing_user_organization_id: UUID | None,
        channel: TicketChannel,
    ) -> None:
        self.organization_id = organization_id
        self.existing_user_organization_id = existing_user_organization_id
        self.channel = channel
        super().__init__(
            "sender already belongs to a different organization "
            f"({existing_user_organization_id}, requested {organization_id}, "
            f"channel={channel.value})"
        )


def normalize_msisdn(raw: str) -> str:
    """Normalize a raw phone number string to E.164
    (`^\\+[1-9][0-9]{6,14}$`): strips spaces/dashes/parens and prepends
    `+` if absent. Raises `ValueError` if the result cannot be normalized
    to that shape."""
    stripped = _MSISDN_STRIP_CHARS.sub("", raw.strip())
    if not stripped:
        raise ValueError(f"cannot normalize phone number: {raw!r}")
    if not stripped.startswith("+"):
        stripped = f"+{stripped}"
    if not _MSISDN_PATTERN.match(stripped):
        raise ValueError(f"cannot normalize phone number to E.164: {raw!r}")
    return stripped


async def _find_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Global (cross-organization), case-insensitive lookup by email —
    see the module docstring for why this is deliberately unscoped."""
    stmt = select(User).where(func.lower(User.email) == email.lower())
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_user_by_phone_number(session: AsyncSession, phone_number: str) -> User | None:
    """Global lookup by (already-unique, E.164) phone number — no
    case-folding needed. Same unscoped rationale as `_find_user_by_email`."""
    stmt = select(User).where(User.phone_number == phone_number)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _resolve_or_provision(
    session: AsyncSession,
    *,
    organization_id: UUID,
    channel: TicketChannel,
    existing_user: User | None,
    build_new_user: "callable[[], User]",
) -> User:
    if existing_user is not None:
        if existing_user.organization_id != organization_id:
            raise CrossOrgSenderConflict(
                organization_id=organization_id,
                existing_user_organization_id=existing_user.organization_id,
                channel=channel,
            )
        return existing_user

    user = build_new_user()
    session.add(user)
    await session.flush()
    return user


async def resolve_email_sender_identity(
    session: AsyncSession,
    *,
    organization_id: UUID,
    email: str,
    display_name: str | None = None,
) -> User:
    """Resolve (or auto-provision) the `User` for an inbound email's
    sender address, within `organization_id` (already resolved from the
    org's `support_email_address` routing).

    Raises `CrossOrgSenderConflict` if `email` already belongs to a user
    in a DIFFERENT organization.
    """
    existing = await _find_user_by_email(session, email)

    def _build_new_user() -> User:
        return User(
            organization_id=organization_id,
            name=display_name or email,
            email=email,
            role=UserRole.TENANT,
            status=UserStatus.PENDING,
            password_hash=None,
            auto_provisioned_channel=TicketChannel.EMAIL,
        )

    return await _resolve_or_provision(
        session,
        organization_id=organization_id,
        channel=TicketChannel.EMAIL,
        existing_user=existing,
        build_new_user=_build_new_user,
    )


async def resolve_whatsapp_sender_identity(
    session: AsyncSession,
    *,
    organization_id: UUID,
    wa_id: str,
    display_name: str | None = None,
) -> User:
    """Resolve (or auto-provision) the `User` for an inbound WhatsApp
    message's sender `wa_id` (the raw Meta-supplied identifier, as it
    appears in `channel_metadata`/webhook payloads — NOT yet E.164
    normalized), within `organization_id` (already resolved from the
    org's `whatsapp_phone_number_id`).

    Raises `CrossOrgSenderConflict` if the normalized phone number already
    belongs to a user in a DIFFERENT organization.
    """
    phone_number = normalize_msisdn(wa_id)
    existing = await _find_user_by_phone_number(session, phone_number)

    def _build_new_user() -> User:
        return User(
            organization_id=organization_id,
            name=display_name or wa_id,
            email=f"wa+{wa_id}@whatsapp.invalid",
            phone_number=phone_number,
            role=UserRole.TENANT,
            status=UserStatus.PENDING,
            password_hash=None,
            auto_provisioned_channel=TicketChannel.WHATSAPP,
        )

    return await _resolve_or_provision(
        session,
        organization_id=organization_id,
        channel=TicketChannel.WHATSAPP,
        existing_user=existing,
        build_new_user=_build_new_user,
    )
