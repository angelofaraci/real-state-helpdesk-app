"""Plain repository for `invite_tokens`.

NOTE: this is deliberately NOT the org-scoped `ScopedRepository` pattern —
that is introduced in a later work unit. Invite tokens have no
organization scoping of their own; they are keyed by user.

"At most one outstanding invite per user" is enforced at the database
level by a partial unique index on `(user_id) WHERE used_at IS NULL`
(see `app/alembic/versions/0001_initial.py`). A second `issue()` call for a
user that already has a pending invite raises `IntegrityError`, which the
caller (the invite-reissue flow) handles by deleting the prior unused row
first via `delete_unused_for_user`.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_opaque_token, sha256_hash
from app.models.invite_token import InviteToken

INVITE_TOKEN_TTL = timedelta(hours=48)


class InviteTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self, user_id: UUID, *, expires_in: timedelta = INVITE_TOKEN_TTL
    ) -> str:
        """Issue a new invite token for `user_id`.

        Returns the raw (unhashed) token to embed in the invite email link.
        Raises `sqlalchemy.exc.IntegrityError` if the user already has an
        outstanding (unused) invite token.
        """
        raw_token = new_opaque_token()
        token = InviteToken(
            user_id=user_id,
            token_hash=sha256_hash(raw_token.encode("utf-8")),
            expires_at=datetime.now(UTC) + expires_in,
        )
        self._session.add(token)
        await self._session.flush()
        return raw_token

    async def consume(self, raw_token: str) -> InviteToken | None:
        """Look up the row for `raw_token` by its SHA-256 hash and lock it
        (`SELECT ... FOR UPDATE`) so concurrent accept-invite attempts on
        the same token serialize instead of racing.

        The caller is responsible for checking `expires_at`/`used_at`.
        """
        digest = sha256_hash(raw_token.encode("utf-8"))
        stmt = (
            select(InviteToken)
            .where(InviteToken.token_hash == digest)
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_used(self, token: InviteToken) -> None:
        """Mark a single invite token row as used."""
        token.used_at = datetime.now(UTC)
        await self._session.flush()

    async def delete_unused_for_user(self, user_id: UUID) -> None:
        """Delete any still-unused invite token row for `user_id`.

        Used by the invite-reissue flow to clear the prior outstanding
        invite before issuing a new one, since the partial unique index
        allows at most one unused row per user at a time.
        """
        stmt = delete(InviteToken).where(
            InviteToken.user_id == user_id, InviteToken.used_at.is_(None)
        )
        await self._session.execute(stmt)
