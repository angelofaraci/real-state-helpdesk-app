"""Rolling-hour per-chat-session rate limit (stage 4 — chatbot).

`check_and_increment` uses a Redis pipeline to atomically `INCR` a
per-session, per-hour counter and (only on the FIRST increment of that
hour bucket, i.e. when the incremented count is `1`) set its expiry with
`NX` so a concurrent request can never reset an already-ticking TTL. The
key embeds the current epoch hour (`chat:rl:{chat_session_id}:{epoch_hour}`)
so the window rolls over naturally every hour without needing a sliding-
window algorithm.

Written against a minimal duck-typed pipeline protocol (`pipeline()` ->
object with `incr`/`expire`/`execute`) rather than importing `redis`
directly, since `redis` is not (yet) an installed dependency of this
project — only `arq`'s bundled client is. Any client satisfying that
protocol (a real `redis.asyncio.Redis`, or a test double) works.
"""

from __future__ import annotations

import time
from typing import Any, Protocol
from uuid import UUID

from app.core.config import get_settings


class ChatRateLimitExceededError(Exception):
    """Raised when a chat session exceeds its rolling-hour message budget."""

    def __init__(self, chat_session_id: UUID, *, limit: int, retry_after: int) -> None:
        self.chat_session_id = chat_session_id
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"chat session {chat_session_id} exceeded rate limit of "
            f"{limit}/hour; retry after {retry_after}s"
        )


class _SupportsPipeline(Protocol):
    def pipeline(self) -> Any: ...


def _rate_limit_key(chat_session_id: UUID, epoch_hour: int) -> str:
    return f"chat:rl:{chat_session_id}:{epoch_hour}"


async def check_and_increment(
    redis: _SupportsPipeline,
    *,
    chat_session_id: UUID,
    limit: int | None = None,
) -> None:
    """Increment `chat_session_id`'s message count for the current rolling
    hour and raise `ChatRateLimitExceededError` if it now exceeds `limit`.

    `limit` defaults to `settings.chat_rate_limit_per_hour`.
    """
    if limit is None:
        limit = get_settings().chat_rate_limit_per_hour

    epoch_hour = int(time.time() // 3600)
    key = _rate_limit_key(chat_session_id, epoch_hour)

    pipe = redis.pipeline()
    pipe.incr(key)
    # 3700s (~1h + buffer) so a bucket always outlives its own hour window,
    # even accounting for clock/scheduling skew right at the boundary.
    pipe.expire(key, 3700, nx=True)
    results = await pipe.execute()
    count = results[0]

    if count > limit:
        # Bucket rolls over at the next epoch-hour boundary.
        retry_after = 3600 - int(time.time() % 3600)
        raise ChatRateLimitExceededError(
            chat_session_id, limit=limit, retry_after=retry_after
        )
