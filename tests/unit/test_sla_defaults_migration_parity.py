"""Unit tests for `app.core.sla_defaults` — the default SLA business-hours
seed data (stage 6 — queues + SLA), mirroring `app.core.taxonomy_defaults`.

Migration 0008 embeds this same JSON shape as an inline `server_default`
literal (a migration can't import the Python module — `server_default`
must be a raw SQL-evaluable expression). This file is the drift guard: it
parses that literal out of the migration source and asserts it is
byte-for-byte identical to `json.dumps(DEFAULT_BUSINESS_HOURS)`.
"""

import ast
import json
from pathlib import Path

from app.core.sla_defaults import DAY_KEYS, DEFAULT_BUSINESS_HOURS, DEFAULT_TIMEZONE

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "app" / "alembic" / "versions" / "0008_queues_sla_stage6.py"


def _inline_business_hours_json_literal() -> str:
    """Find `sa.text("'{...}'::jsonb")` in the migration and return the
    `{...}` JSON text. Uses `ast` (not a regex): the source literal is
    split across several implicitly-concatenated, escaped string
    constants, which `ast` evaluates the same way Python itself would."""
    source = MIGRATION_PATH.read_text()
    tree = ast.parse(source, filename=str(MIGRATION_PATH))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "text"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.startswith("'{")
            and node.args[0].value.endswith("}'::jsonb")
        ):
            literal: str = node.args[0].value
            return literal[1 : -len("'::jsonb")]

    raise AssertionError(
        "expected an inline business_hours JSONB server_default literal "
        f"(sa.text(\"'{{...}}'::jsonb\")) in {MIGRATION_PATH}"
    )


def test_default_timezone_is_buenos_aires() -> None:
    assert DEFAULT_TIMEZONE == "America/Argentina/Buenos_Aires"


def test_day_keys_are_ordered_to_match_date_weekday_index() -> None:
    assert DAY_KEYS == ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def test_default_business_hours_is_monday_to_friday_nine_to_six() -> None:
    assert DEFAULT_BUSINESS_HOURS == {
        "mon": [["09:00", "18:00"]],
        "tue": [["09:00", "18:00"]],
        "wed": [["09:00", "18:00"]],
        "thu": [["09:00", "18:00"]],
        "fri": [["09:00", "18:00"]],
    }
    # sat/sun keys are absent — absence means "closed", not an empty list.
    assert "sat" not in DEFAULT_BUSINESS_HOURS
    assert "sun" not in DEFAULT_BUSINESS_HOURS


def test_migration_business_hours_json_literal_byte_matches_sla_defaults() -> None:
    inline_json = _inline_business_hours_json_literal()

    assert inline_json == json.dumps(DEFAULT_BUSINESS_HOURS, separators=(",", ":"))
    assert json.loads(inline_json) == DEFAULT_BUSINESS_HOURS
