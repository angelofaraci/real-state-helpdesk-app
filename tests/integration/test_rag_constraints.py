"""Integration tests for Stage 3 (RAG) real-Postgres-only behaviors,
following the same skip-stub pattern as
`tests/integration/test_classification_constraints.py`.

Static `Base.metadata` inspection (`tests/unit/test_knowledge_chunk_model.py`,
`test_knowledge_base_model.py`, `test_ticket_embedding_model.py`) proves the
CHECK constraints and unique indexes are *declared*. It does NOT prove that:

- `ck_knowledge_chunks_content_not_blank` and
  `ck_knowledge_chunks_token_count_positive` are actually enforced by
  Postgres when inserted directly (bypassing any future
  service/repository-level validation).
- `ck_knowledge_base_status_known` and
  `ck_knowledge_base_failed_requires_embedding_error` are actually enforced.
- The unique index on `ticket_embeddings.ticket_id` actually rejects a
  second INSERT for the same `ticket_id` (the idempotency guarantee a
  future re-embedding worker relies on) with an `IntegrityError` /
  `UniqueViolationError`.

No live Postgres is reachable in this sandbox. These tests are
intentionally skipped rather than faking Postgres-specific behavior with
SQLite. They document the exact scope that remains unverified against a
real database and should be run manually or in CI against
`docker-compose up -d postgres` once a Postgres integration test harness
exists.
"""

import pytest


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_knowledge_chunk_rejects_blank_content_and_non_positive_token_count() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Insert a `knowledge_chunks` row with blank/whitespace-only `content`.
       Assert it raises an `IntegrityError` / `CheckViolationError` from
       `ck_knowledge_chunks_content_not_blank`.
    2. Insert a `knowledge_chunks` row with `token_count <= 0`. Assert it
       raises an `IntegrityError` / `CheckViolationError` from
       `ck_knowledge_chunks_token_count_positive`.
    3. Assert a row with non-blank content and a positive token_count
       inserts successfully.
    """
    raise NotImplementedError("run against a live Postgres instance")


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_knowledge_base_status_check_and_failed_requires_embedding_error() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Insert a `knowledge_base` row with `status` outside
       ('pending', 'processing', 'ready', 'failed'). Assert an
       `IntegrityError` / `CheckViolationError`.
    2. Insert a `knowledge_base` row with `status='failed'` and
       `embedding_error=NULL`. Assert an `IntegrityError` /
       `CheckViolationError` from
       `ck_knowledge_base_failed_requires_embedding_error`.
    3. Assert `status='failed'` with a non-null `embedding_error` inserts
       successfully, and any non-'failed' status with `embedding_error=NULL`
       also inserts successfully.
    """
    raise NotImplementedError("run against a live Postgres instance")


@pytest.mark.skip(
    reason="requires live PostgreSQL — no integration harness configured yet"
)
async def test_ticket_embedding_unique_ticket_id_rejects_duplicate_insert() -> None:
    """Placeholder for the real assertion against Postgres:

    1. Insert a `ticket_embeddings` row for a given `ticket_id`. Commit.
    2. Insert a second `ticket_embeddings` row for the *same* `ticket_id`.
       Assert it raises an `IntegrityError` / `UniqueViolationError` against
       the unique index on `ticket_id`, proving a re-embedding worker
       cannot silently create duplicate embeddings for one ticket.
    """
    raise NotImplementedError("run against a live Postgres instance")
