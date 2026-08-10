"""Bootstrap script for the platform's single super-admin user.

A super-admin is `role=admin, organization_id=NULL`. This script is the
ONLY way a super-admin is ever created in this system: there is no API
endpoint for it, since super-admins are platform operators rather than
something any org admin can invite. From there, the super-admin logs in
normally via `POST /auth/login` and uses `POST /organizations` to create
organizations, then invites each org's first real admin through the
normal invite flow.

Run as:

    python -m app.core.seed
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.core.session import get_session
from app.models.enums import UserRole, UserStatus
from app.models.user import User


async def _find_existing_super_admin(session: AsyncSession) -> User | None:
    result = await session.execute(
        select(User).where(User.organization_id.is_(None), User.role == UserRole.ADMIN)
    )
    return result.scalar_one_or_none()


async def seed_super_admin(
    session: AsyncSession, *, email: str, password: str, name: str = "Super Admin"
) -> User:
    """Idempotently ensure the platform super-admin exists.

    Looks up an existing user by `role=admin AND organization_id IS NULL`
    (there should only ever be one) rather than by email. If one already
    exists, it is returned unchanged — the password is deliberately NOT
    re-hashed/re-set on an existing super-admin. Otherwise a new one is
    created, added to `session`, and flushed.
    """
    admin = await _find_existing_super_admin(session)
    if admin is not None:
        return admin

    admin = User(
        organization_id=None,
        name=name,
        email=email,
        role=UserRole.ADMIN,
        password_hash=hash_password(password),
        status=UserStatus.ACTIVE,
    )
    session.add(admin)
    await session.flush()
    return admin


async def main() -> None:
    """CLI entrypoint: read `SUPER_ADMIN_*` env vars and seed the super-admin."""
    settings = get_settings()
    if not settings.super_admin_email or not settings.super_admin_password:
        print(
            "SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD must both be set "
            "(as environment variables or in .env) to seed the super-admin.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    async for session in get_session():
        already_existed = await _find_existing_super_admin(session) is not None

        admin = await seed_super_admin(
            session,
            email=settings.super_admin_email,
            password=settings.super_admin_password,
            name=settings.super_admin_name,
        )
        await session.commit()

        status = "already existed" if already_existed else "created"
        print(f"Super-admin {admin.email} {status}.")
        break


if __name__ == "__main__":
    asyncio.run(main())
