"""Tests verifying migration 0004 (stage-3 RAG: knowledge_base,
knowledge_chunks, ticket_embeddings, messages.based_on_suggestion_id).

Same offline-SQL-mode strategy as `test_alembic_migration.py` /
`test_migration_0003.py`: render SQL via `alembic upgrade head --sql` /
`alembic downgrade ... --sql` without a live Postgres connection, and assert
on the exact rendered SQL strings.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic_offline_sql() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_migration_0004_offline_sql_generation_succeeds() -> None:
    result = _run_alembic_offline_sql()

    assert result.returncode == 0, (
        f"alembic upgrade head --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )


def test_migration_0004_creates_knowledge_base_table() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TABLE knowledge_base" in sql
    assert "ck_knowledge_base_title_not_blank" in sql
    assert "ck_knowledge_base_content_not_blank" in sql
    assert "ck_knowledge_base_status_known" in sql
    assert "ck_knowledge_base_failed_requires_embedding_error" in sql


def test_migration_0004_creates_knowledge_chunks_table_with_check_constraints() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TABLE knowledge_chunks" in sql
    assert "ck_knowledge_chunks_content_not_blank" in sql
    assert "ck_knowledge_chunks_token_count_positive" in sql
    assert (
        "CREATE UNIQUE INDEX ix_knowledge_chunks_kb_id_chunk_index "
        "ON knowledge_chunks (knowledge_base_id, chunk_index)" in sql
    )
    assert (
        "CREATE INDEX ix_knowledge_chunks_organization_id ON knowledge_chunks "
        "(organization_id)" in sql
    )


def test_migration_0004_creates_ticket_embeddings_table_with_unique_ticket_id() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "CREATE TABLE ticket_embeddings" in sql
    assert "ck_ticket_embeddings_summary_not_blank" in sql
    assert (
        "CREATE UNIQUE INDEX ix_ticket_embeddings_ticket_id_unique "
        "ON ticket_embeddings (ticket_id)" in sql
    )


def test_migration_0004_creates_hnsw_indexes_on_both_embedding_columns() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert (
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        in sql
    )
    assert (
        "CREATE INDEX ix_ticket_embeddings_embedding_hnsw ON ticket_embeddings "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        in sql
    )


def test_migration_0004_adds_messages_based_on_suggestion_id_fk_with_check() -> None:
    result = _run_alembic_offline_sql()
    sql = result.stdout

    assert "ALTER TABLE messages ADD COLUMN based_on_suggestion_id" in sql
    assert "ck_messages_based_on_suggestion_not_self" in sql
    assert "based_on_suggestion_id <> id" in sql
    assert "REFERENCES messages (id)" in sql
    assert "SET NULL" in sql


def test_migration_0004_downgrade_drops_hnsw_indexes_before_tables() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0004:0003", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0004:0003 --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP INDEX ix_knowledge_chunks_embedding_hnsw" in sql
    assert "DROP INDEX ix_ticket_embeddings_embedding_hnsw" in sql
    assert "DROP TABLE knowledge_chunks" in sql
    assert "DROP TABLE ticket_embeddings" in sql
    assert "DROP TABLE knowledge_base" in sql
    assert "ALTER TABLE messages DROP CONSTRAINT ck_messages_based_on_suggestion_not_self" in sql
    assert "ALTER TABLE messages DROP COLUMN based_on_suggestion_id" in sql

    hnsw_kc_drop = sql.index("DROP INDEX ix_knowledge_chunks_embedding_hnsw")
    hnsw_te_drop = sql.index("DROP INDEX ix_ticket_embeddings_embedding_hnsw")
    kc_table_drop = sql.index("DROP TABLE knowledge_chunks")
    te_table_drop = sql.index("DROP TABLE ticket_embeddings")
    kb_table_drop = sql.index("DROP TABLE knowledge_base")

    assert hnsw_kc_drop < kc_table_drop
    assert hnsw_te_drop < te_table_drop
    # knowledge_chunks (child, FK -> knowledge_base) must be dropped before
    # knowledge_base (parent) — though CASCADE would also allow either
    # order, we keep child-before-parent for clarity/consistency.
    assert kc_table_drop < kb_table_drop


def test_migration_0004_downgrade_all_the_way_still_succeeds() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0004:base", "--sql"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"alembic downgrade 0004:base --sql failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    sql = result.stdout

    assert "DROP TABLE knowledge_base" in sql
    assert "DROP TABLE organizations" in sql
