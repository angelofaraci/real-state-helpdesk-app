"""Unit tests for `app.services.channel_metadata` — pure helpers for
building/reading the `channel_metadata` JSONB columns added to `tickets`
and `chat_sessions` by migration 0007 (stage 5 — multichannel).

Every helper must return a NEW dict rather than mutating its input, since
SQLAlchemy's JSONB type has no in-place nested-mutation change tracking —
mutating the existing dict in place would silently fail to persist.
"""

from app.services.channel_metadata import contains, with_appended, with_values


def test_with_appended_creates_the_list_when_key_absent() -> None:
    result = with_appended(None, "message_ids", "msg-1")

    assert result == {"message_ids": ["msg-1"]}


def test_with_appended_appends_to_an_existing_list() -> None:
    original = {"message_ids": ["msg-1"]}

    result = with_appended(original, "message_ids", "msg-2")

    assert result == {"message_ids": ["msg-1", "msg-2"]}


def test_with_appended_does_not_mutate_the_original_dict() -> None:
    original = {"message_ids": ["msg-1"]}

    result = with_appended(original, "message_ids", "msg-2")

    assert original == {"message_ids": ["msg-1"]}
    assert result is not original
    assert result["message_ids"] is not original["message_ids"]


def test_with_appended_trims_to_the_cap_keeping_the_most_recent() -> None:
    original = {"message_ids": ["msg-1", "msg-2", "msg-3"]}

    result = with_appended(original, "message_ids", "msg-4", cap=3)

    assert result == {"message_ids": ["msg-2", "msg-3", "msg-4"]}


def test_with_appended_preserves_other_keys() -> None:
    original = {"wa_id": "1234567890", "message_ids": ["msg-1"]}

    result = with_appended(original, "message_ids", "msg-2")

    assert result["wa_id"] == "1234567890"


def test_with_values_merges_updates_into_a_new_dict() -> None:
    original = {"wa_id": "1234567890"}

    result = with_values(original, subject="Re: leak")

    assert result == {"wa_id": "1234567890", "subject": "Re: leak"}
    assert original == {"wa_id": "1234567890"}
    assert result is not original


def test_with_values_handles_a_none_metadata() -> None:
    result = with_values(None, wa_id="1234567890")

    assert result == {"wa_id": "1234567890"}


def test_with_values_overwrites_existing_keys() -> None:
    original = {"subject": "old"}

    result = with_values(original, subject="new")

    assert result == {"subject": "new"}


def test_contains_true_when_value_present_in_list() -> None:
    metadata = {"message_ids": ["msg-1", "msg-2"]}

    assert contains(metadata, "message_ids", "msg-2") is True


def test_contains_false_when_value_absent() -> None:
    metadata = {"message_ids": ["msg-1"]}

    assert contains(metadata, "message_ids", "msg-99") is False


def test_contains_false_when_key_absent() -> None:
    assert contains({}, "message_ids", "msg-1") is False


def test_contains_false_when_metadata_is_none() -> None:
    assert contains(None, "message_ids", "msg-1") is False
