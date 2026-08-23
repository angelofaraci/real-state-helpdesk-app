"""Unauthenticated liveness/readiness probes (stage 8 — devops).

`/health` is a pure liveness check with zero I/O: it returns 200 whenever
the process is alive and able to handle a request at all, regardless of
the state of any downstream dependency. Docker `HEALTHCHECK` directives
and CI smoke tests rely on this endpoint being reachable without a JWT.

`/ready` is a readiness check: it exercises the exact same `get_session`/
`get_redis` dependencies every other route uses (no bespoke connection
mechanism) and reports 503 with the failing dependency named in the body
when Postgres or Redis is unreachable.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps_chat import get_redis
from app.core.session import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Deliberately does not touch the DB session or Redis
    dependency — see module docstring."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis),
) -> dict[str, object]:
    """Readiness probe. Checks Postgres (`SELECT 1`) and Redis (`PING`) and
    returns 503 naming whichever dependency failed if either is
    unreachable."""
    checks = {"database": "ok", "redis": "ok"}

    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = "error"

    try:
        await redis.ping()
    except Exception:
        checks["redis"] = "error"

    if "error" in checks.values():
        response.status_code = 503
        return {"status": "not_ready", "checks": checks}

    return {"status": "ready", "checks": checks}
