"""Unit tests for `app.services.chat_rate_limit` — the rolling-hour chat
message rate limit (stage 4 — chatbot).

No real Redis is available in this sandbox (the `redis` package is not
even an installed dependency — `arq` brings its own bundled client, but
this service is written against a minimal duck-typed pipeline protocol so
either a real `redis.asyncio.Redis` or this in-memory fake works). The fake
below implements just the two pipelined commands `check_and_increment`
needs (`incr`, `expire`) plus `execute`, mirroring how a real async Redis
pipeline batches commands and returns their results as a list.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.chat_rate_limit import ChatRateLimitExceededError, check_and_increment


class _FakePipeline:
    def __init__(self, store: dict[str, int], ttls: dict[str, int]) -> None:
        self._store = store
        self._ttls = ttls
        self._ops: list[tuple[str, tuple, dict]] = []

    def incr(self, key: str) -> "_FakePipeline":
        self._ops.append(("incr", (key,), {}))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> "_FakePipeline":
        self._ops.append(("expire", (key, seconds), {"nx": nx}))
        return self

    async def execute(self) -> list[int | bool]:
        results: list[int | bool] = []
        for op, args, kwargs in self._ops:
            if op == "incr":
                (key,) = args
                self._store[key] = self._store.get(key, 0) + 1
                results.append(self._store[key])
            elif op == "expire":
                key, seconds = args
                nx = kwargs.get("nx", False)
                if nx and key in self._ttls:
                    results.append(False)
                else:
                    self._ttls[key] = seconds
                    results.append(True)
        self._ops = []
        return results

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeRedis:
    """Minimal in-memory stand-in for the subset of `redis.asyncio.Redis`
    that `check_and_increment` uses."""

    def __init__(self) -> None:
        self._store: dict[str, int] = {}
        self._ttls: dict[str, int] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self._store, self._ttls)


async def test_allows_calls_up_to_the_limit() -> None:
    redis = FakeRedis()
    session_id = uuid4()

    for _ in range(30):
        await check_and_increment(redis, chat_session_id=session_id, limit=30)


async def test_raises_on_the_call_after_the_limit_within_the_same_hour() -> None:
    redis = FakeRedis()
    session_id = uuid4()

    for _ in range(30):
        await check_and_increment(redis, chat_session_id=session_id, limit=30)

    with pytest.raises(ChatRateLimitExceededError):
        await check_and_increment(redis, chat_session_id=session_id, limit=30)


async def test_key_format_includes_session_id_and_epoch_hour() -> None:
    import time

    redis = FakeRedis()
    session_id = uuid4()
    epoch_hour = int(time.time() // 3600)

    await check_and_increment(redis, chat_session_id=session_id, limit=30)

    expected_key = f"chat:rl:{session_id}:{epoch_hour}"
    assert expected_key in redis._store
    assert redis._store[expected_key] == 1


async def test_different_sessions_are_tracked_independently() -> None:
    redis = FakeRedis()
    session_a = uuid4()
    session_b = uuid4()

    for _ in range(30):
        await check_and_increment(redis, chat_session_id=session_a, limit=30)

    # session_b has its own independent budget, unaffected by session_a's.
    await check_and_increment(redis, chat_session_id=session_b, limit=30)
