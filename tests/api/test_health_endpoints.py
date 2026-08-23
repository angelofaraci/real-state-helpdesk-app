"""API-level tests for the unauthenticated `/health` and `/ready` probes
(stage 8 — devops, PR5).

`/health` is a pure liveness probe: it must do zero I/O, so it stays 200
even when the DB/Redis dependencies are overridden to raise (proven below
via `app.dependency_overrides`, the same mechanism `tests/integration/
test_chat_flow.py`'s `wired_app` fixture uses).

`/ready` is a readiness probe: it must reach Postgres (`SELECT 1`) and
Redis (`PING`) and report 503 with the failing dependency named in the
body when either is unreachable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps_chat import get_redis
from app.core.session import get_session
from app.main import app


class _RaisingSession:
    async def execute(self, _stmt):
        raise RuntimeError("database should not be touched by /health")


class _RaisingRedis:
    async def ping(self) -> bool:
        raise RuntimeError("redis should not be touched by /health")


class _OkSession:
    async def execute(self, _stmt):
        return None


class _OkRedis:
    async def ping(self) -> bool:
        return True


class _FailingSession:
    async def execute(self, _stmt):
        raise ConnectionError("postgres unreachable")


class _FailingRedis:
    async def ping(self) -> bool:
        raise ConnectionError("redis unreachable")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def test_health_returns_200_without_touching_dependencies() -> None:
    app.dependency_overrides[get_session] = lambda: _RaisingSession()
    app.dependency_overrides[get_redis] = lambda: _RaisingRedis()

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_returns_200_when_both_dependencies_are_reachable() -> None:
    app.dependency_overrides[get_session] = lambda: _OkSession()
    app.dependency_overrides[get_redis] = lambda: _OkRedis()

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "ok"


def test_ready_returns_503_when_postgres_unreachable() -> None:
    app.dependency_overrides[get_session] = lambda: _FailingSession()
    app.dependency_overrides[get_redis] = lambda: _OkRedis()

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error"
    assert body["checks"]["redis"] == "ok"


def test_ready_returns_503_when_redis_unreachable() -> None:
    app.dependency_overrides[get_session] = lambda: _OkSession()
    app.dependency_overrides[get_redis] = lambda: _FailingRedis()

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["redis"] == "error"


def test_health_and_ready_are_unauthenticated() -> None:
    app.dependency_overrides[get_session] = lambda: _OkSession()
    app.dependency_overrides[get_redis] = lambda: _OkRedis()

    with TestClient(app) as client:
        health_response = client.get("/health")
        ready_response = client.get("/ready")

    assert health_response.status_code == 200
    assert ready_response.status_code == 200
