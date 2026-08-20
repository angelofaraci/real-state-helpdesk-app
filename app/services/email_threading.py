"""Pure email-thread matching helpers (stage 5 — multichannel, PR2 — email
inbound). No DB/I-O: everything here is a plain string/list transform, so
`app.services.email_ingestion`'s matching ladder (design ADR-7) can stay
fully unit-testable without a session.

- `normalize_subject` — a stable dedup key for the sender+subject fallback
  match (step 3 of the ladder).
- `extract_reference_ids` — the ordered candidate list for the
  In-Reply-To/References match (step 1), most recent ancestor first.
- `parse_ticket_tag` — extracts the `[#xxxxxxxx]` ticket-id prefix tag for
  the subject-tag match (step 2).
- `build_reply_headers` — builds the `Subject`/`In-Reply-To`/`References`
  headers for an OUTBOUND reply. Implemented here (rather than in a PR3
  module) since it shares `normalize_subject`'s tag-detection logic and PR3
  (email outbound) reuses it as-is.
"""

from __future__ import annotations

import re
from uuid import UUID

# Case-insensitive, with or without a following colon/space: "Re:", "RE ",
# "Fwd:", "FW:", etc. Applied repeatedly (see `normalize_subject`) so a
# subject with multiple stacked prefixes ("Re: Fwd: RE: ...") is fully
# unwrapped, not just once.
_REPLY_FORWARD_PREFIX_RE = re.compile(r"^(re|fwd|fw)\s*:?\s*", re.IGNORECASE)

# The ticket-tag pattern a reply's Subject line carries: `[#` + exactly 8
# lowercase hex chars (the first 8 chars of a ticket's UUID, which are
# always hex regardless of case — `str(uuid.uuid4())` is always lowercase)
# + `]`.
_TICKET_TAG_RE = re.compile(r"\[#([0-9a-f]{8})\]")

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_subject(subject: str) -> str:
    """Return a stable, comparable form of an email subject line: strip
    every leading Re:/Fwd:/RE:/FW: prefix (repeatedly — see
    `_REPLY_FORWARD_PREFIX_RE`), remove the `[#xxxxxxxx]` ticket tag, collapse
    internal whitespace, and lowercase. Idempotent: normalizing an
    already-normalized subject returns it unchanged."""
    text = subject
    while True:
        stripped = _REPLY_FORWARD_PREFIX_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped

    text = _TICKET_TAG_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text.lower()


def extract_reference_ids(in_reply_to: str | None, references: list[str]) -> list[str]:
    """Return the ordered list of candidate ancestor Message-IDs to try
    when matching an inbound reply to an existing thread (ladder step 1):
    `in_reply_to` first (the DIRECT parent, if any), then `references`
    reversed (RFC 5322's `References` header is oldest-first, so reversing
    it walks from the most recent ancestor backward). Deduplicated
    (keeping each id's first/most-recent occurrence), dropping any
    `None`/empty entries."""
    candidates = [in_reply_to, *reversed(references)]

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def parse_ticket_tag(subject: str) -> str | None:
    """Extract the 8-hex-char ticket-id prefix from a `[#xxxxxxxx]` tag in
    `subject` (ladder step 2), or `None` if no such tag is present."""
    match = _TICKET_TAG_RE.search(subject)
    if match is None:
        return None
    return match.group(1)


def build_reply_headers(
    *,
    ticket_id: UUID,
    original_subject: str,
    in_reply_to_message_id: str,
    references_message_ids: list[str],
) -> dict[str, str]:
    """Build the `Subject`/`In-Reply-To`/`References` headers for an
    OUTBOUND reply on `ticket_id` (reused by PR3 — email outbound; defined
    here since it shares this module's tag-detection logic).

    - `Subject`: `original_subject`, tagged with `[#<first 8 hex chars of
      ticket_id>]` UNLESS it is already tagged (never double-tagged).
    - `In-Reply-To`: `in_reply_to_message_id` verbatim.
    - `References`: `references_message_ids` (the prior thread's
      references) with `in_reply_to_message_id` appended if not already
      present, space-joined — the standard RFC 5322 shape for a reply's
      References header (every prior ancestor, oldest first, plus the
      message being replied to).
    """
    tag = str(ticket_id)[:8]
    if parse_ticket_tag(original_subject) is None:
        subject = f"{original_subject} [#{tag}]"
    else:
        subject = original_subject

    references = list(references_message_ids)
    if in_reply_to_message_id not in references:
        references.append(in_reply_to_message_id)

    return {
        "Subject": subject,
        "In-Reply-To": in_reply_to_message_id,
        "References": " ".join(references),
    }
