"""Pure helpers for building/reading the `channel_metadata` JSONB columns
added to `tickets` and `chat_sessions` by migration 0007 (stage 5 —
multichannel): inbound-message dedup ids, the WhatsApp `wa_id`, the email
`Message-ID`/subject thread key, and similar per-channel bookkeeping.

No DB access, no mutation of the input dict — every function returns a
brand NEW dict. This is deliberate: SQLAlchemy's JSONB column type has no
in-place nested-mutation change tracking, so `metadata["key"].append(x)`
on an already-loaded ORM attribute would silently never be persisted. The
correct pattern is always `row.channel_metadata = with_appended(row.channel_metadata, ...)`
— a fresh top-level object assignment SQLAlchemy's change tracking does
detect.
"""

from __future__ import annotations

from typing import Any


def with_appended(
    metadata: dict[str, Any] | None,
    key: str,
    value: Any,
    *,
    cap: int | None = None,
) -> dict[str, Any]:
    """Return a NEW dict equal to `metadata` with `value` appended to the
    list stored at `key` (creating that list if `metadata`/`key` is
    absent). If `cap` is given, the resulting list is trimmed to its last
    `cap` items (most recent kept)."""
    result = dict(metadata) if metadata else {}
    existing = list(result.get(key, []))
    existing.append(value)
    if cap is not None:
        existing = existing[-cap:]
    result[key] = existing
    return result


def with_values(metadata: dict[str, Any] | None, **updates: Any) -> dict[str, Any]:
    """Return a NEW dict equal to `metadata` with `updates` merged in
    (overwriting any existing keys of the same name)."""
    result = dict(metadata) if metadata else {}
    result.update(updates)
    return result


def contains(metadata: dict[str, Any] | None, key: str, value: Any) -> bool:
    """Whether `value` is present in the list stored at `key`. `False` if
    `metadata` is `None` or has no such key."""
    if not metadata:
        return False
    return value in metadata.get(key, [])
