"""Plain repository for `refresh_tokens` rotation.

NOTE: this is deliberately NOT the org-scoped `ScopedRepository` pattern —
that is introduced in a later work unit. Refresh tokens have no
organization scoping of their own; they are keyed by user.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_opaque_token, sha256_hash
from app.models.refresh_token import RefreshToken

DEFAULT_REFRESH_TOKEN_TTL = timedelta(days=30)


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def issue(
        self,
        user_id: UUID,
        *,
        family_id: UUID | None = None,
        expires_in: timedelta = DEFAULT_REFRESH_TOKEN_TTL,
    ) -> tuple[str, UUID]:
        """Issue a new refresh token, optionally continuing an existing family.

        Returns the raw (unhashed) token to hand to the client, and the
        family id it belongs to.
        """
        raw_token = new_opaque_token()
        resolved_family_id = family_id or uuid4()
        token = RefreshToken(
            user_id=user_id,
            token_hash=sha256_hash(raw_token.encode("utf-8")),
            family_id=resolved_family_id,
            expires_at=datetime.now(UTC) + expires_in,
        )
        self._session.add(token)
        await self._session.flush()
        return raw_token, resolved_family_id

    async def rotate(self, raw_token: str) -> RefreshToken | None:
        """Look up the row for `raw_token` by its SHA-256 hash and lock it
        (`SELECT ... FOR UPDATE`) so concurrent rotation attempts on the
        same token serialize instead of racing.
        """
        digest = sha256_hash(raw_token.encode("utf-8"))
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.token_hash == digest)
            .with_for_update()
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def revoke(self, token: RefreshToken) -> None:
        """Mark a single row as revoked."""
        token.revoked_at = datetime.now(UTC)
        await self._session.flush()

    async def revoke_family(self, family_id: UUID) -> None:
        """Revoke every still-active row in a token family (reuse detection
        / logout)."""
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await self._session.execute(stmt)
