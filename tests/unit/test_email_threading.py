"""Unit tests for `app.services.email_threading` — pure email-thread
matching helpers (stage 5 — multichannel, PR2 — email inbound).

No DB/I-O: `normalize_subject`, `extract_reference_ids`, `parse_ticket_tag`,
and `build_reply_headers` are all pure functions, so no fixtures are
needed.
"""

from __future__ import annotations

from uuid import UUID

from app.services.email_threading import (
    build_reply_headers,
    extract_reference_ids,
    normalize_subject,
    parse_ticket_tag,
)

# ---------------------------------------------------------------------------
# normalize_subject
# ---------------------------------------------------------------------------


def test_normalize_subject_lowercases_and_collapses_whitespace() -> None:
    assert normalize_subject("  Leaking   Faucet  ") == "leaking faucet"


def test_normalize_subject_strips_a_single_re_prefix() -> None:
    assert normalize_subject("Re: Leaking faucet") == "leaking faucet"


def test_normalize_subject_strips_re_prefix_without_colon() -> None:
    assert normalize_subject("Re Leaking faucet") == "leaking faucet"


def test_normalize_subject_strips_repeated_re_and_fwd_prefixes() -> None:
    assert normalize_subject("Re: Fwd: RE: FW: Leaking faucet") == "leaking faucet"


def test_normalize_subject_strips_prefixes_case_insensitively() -> None:
    assert normalize_subject("rE: fwd: leaking faucet") == "leaking faucet"


def test_normalize_subject_removes_ticket_tag() -> None:
    assert normalize_subject("Leaking faucet [#a1b2c3d4]") == "leaking faucet"


def test_normalize_subject_removes_ticket_tag_and_prefixes_together() -> None:
    assert normalize_subject("Re: Leaking faucet [#a1b2c3d4]") == "leaking faucet"


def test_normalize_subject_is_idempotent() -> None:
    once = normalize_subject("Re: Fwd: Leaking Faucet [#a1b2c3d4]")
    twice = normalize_subject(once)
    assert once == twice


def test_normalize_subject_handles_empty_string() -> None:
    assert normalize_subject("") == ""


# ---------------------------------------------------------------------------
# extract_reference_ids
# ---------------------------------------------------------------------------


def test_extract_reference_ids_puts_in_reply_to_first() -> None:
    result = extract_reference_ids("<msg-3@x>", ["<msg-1@x>", "<msg-2@x>"])
    assert result == ["<msg-3@x>", "<msg-2@x>", "<msg-1@x>"]


def test_extract_reference_ids_reverses_references_most_recent_first() -> None:
    result = extract_reference_ids(None, ["<msg-1@x>", "<msg-2@x>", "<msg-3@x>"])
    assert result == ["<msg-3@x>", "<msg-2@x>", "<msg-1@x>"]


def test_extract_reference_ids_dedupes_while_preserving_first_occurrence_order() -> None:
    result = extract_reference_ids("<msg-2@x>", ["<msg-1@x>", "<msg-2@x>"])
    assert result == ["<msg-2@x>", "<msg-1@x>"]


def test_extract_reference_ids_drops_none_and_empty_entries() -> None:
    result = extract_reference_ids(None, ["", "<msg-1@x>", ""])
    assert result == ["<msg-1@x>"]


def test_extract_reference_ids_returns_empty_list_when_nothing_given() -> None:
    assert extract_reference_ids(None, []) == []


# ---------------------------------------------------------------------------
# parse_ticket_tag
# ---------------------------------------------------------------------------


def test_parse_ticket_tag_extracts_the_hex_prefix() -> None:
    assert parse_ticket_tag("Re: Leaking faucet [#a1b2c3d4]") == "a1b2c3d4"


def test_parse_ticket_tag_returns_none_when_absent() -> None:
    assert parse_ticket_tag("Leaking faucet") is None


def test_parse_ticket_tag_requires_exactly_eight_hex_chars() -> None:
    assert parse_ticket_tag("Leaking faucet [#a1b2c3]") is None
    assert parse_ticket_tag("Leaking faucet [#a1b2c3d4e5]") is None


def test_parse_ticket_tag_rejects_non_hex_characters() -> None:
    assert parse_ticket_tag("Leaking faucet [#zzzzzzzz]") is None


# ---------------------------------------------------------------------------
# build_reply_headers
# ---------------------------------------------------------------------------


def test_build_reply_headers_tags_the_subject_with_the_ticket_id_prefix() -> None:
    ticket_id = UUID("a1b2c3d4-0000-0000-0000-000000000000")

    headers = build_reply_headers(
        ticket_id=ticket_id,
        original_subject="Leaking faucet",
        in_reply_to_message_id="<inbound-1@example.com>",
        references_message_ids=["<inbound-0@example.com>"],
    )

    assert headers["Subject"] == "Leaking faucet [#a1b2c3d4]"


def test_build_reply_headers_does_not_double_tag_an_already_tagged_subject() -> None:
    ticket_id = UUID("a1b2c3d4-0000-0000-0000-000000000000")

    headers = build_reply_headers(
        ticket_id=ticket_id,
        original_subject="Leaking faucet [#a1b2c3d4]",
        in_reply_to_message_id="<inbound-1@example.com>",
        references_message_ids=[],
    )

    assert headers["Subject"] == "Leaking faucet [#a1b2c3d4]"


def test_build_reply_headers_sets_in_reply_to() -> None:
    ticket_id = UUID("a1b2c3d4-0000-0000-0000-000000000000")

    headers = build_reply_headers(
        ticket_id=ticket_id,
        original_subject="Leaking faucet",
        in_reply_to_message_id="<inbound-1@example.com>",
        references_message_ids=[],
    )

    assert headers["In-Reply-To"] == "<inbound-1@example.com>"


def test_build_reply_headers_joins_references_with_spaces() -> None:
    ticket_id = UUID("a1b2c3d4-0000-0000-0000-000000000000")

    headers = build_reply_headers(
        ticket_id=ticket_id,
        original_subject="Leaking faucet",
        in_reply_to_message_id="<inbound-2@example.com>",
        references_message_ids=["<inbound-0@example.com>", "<inbound-1@example.com>"],
    )

    assert headers["References"] == (
        "<inbound-0@example.com> <inbound-1@example.com> <inbound-2@example.com>"
    )


def test_build_reply_headers_does_not_duplicate_in_reply_to_in_references() -> None:
    ticket_id = UUID("a1b2c3d4-0000-0000-0000-000000000000")

    headers = build_reply_headers(
        ticket_id=ticket_id,
        original_subject="Leaking faucet",
        in_reply_to_message_id="<inbound-1@example.com>",
        references_message_ids=["<inbound-0@example.com>", "<inbound-1@example.com>"],
    )

    assert headers["References"] == "<inbound-0@example.com> <inbound-1@example.com>"
