"""Adoption-guard test: `openai.AsyncOpenAI(...)`/`AsyncOpenAI(...)` must
be constructed in exactly one place — `app/services/llm_client.py` — so
every LLM/embedding call in this codebase goes through
`InstrumentedAsyncOpenAI` and is therefore cost/latency-instrumented.

Walks the `app/` source tree with `ast` (no import side effects, no
false positives from string mentions in docstrings/comments) looking
for a `Call` node whose function is `openai.AsyncOpenAI` (attribute
access after `import openai`) or bare `AsyncOpenAI` (after
`from openai import AsyncOpenAI`).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
ALLOWED_FILE = APP_ROOT / "services" / "llm_client.py"


class _AsyncOpenAIConstructionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.found = False

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "AsyncOpenAI":
            self.found = True
        elif isinstance(func, ast.Name) and func.id == "AsyncOpenAI":
            self.found = True
        self.generic_visit(node)


def _constructs_async_openai(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _AsyncOpenAIConstructionVisitor()
    visitor.visit(tree)
    return visitor.found


def test_async_openai_constructed_only_in_llm_client_module() -> None:
    offending_files = [
        path
        for path in APP_ROOT.rglob("*.py")
        if path != ALLOWED_FILE and _constructs_async_openai(path)
    ]

    assert offending_files == [], (
        "openai.AsyncOpenAI(...)/AsyncOpenAI(...) must only be constructed in "
        f"{ALLOWED_FILE}; found it also in: {offending_files}. Use "
        "app.services.llm_client.build_llm_client(...) instead so calls are "
        "cost/latency instrumented."
    )


def test_llm_client_module_itself_constructs_async_openai() -> None:
    # sanity check: the guard isn't vacuously true because nobody
    # constructs AsyncOpenAI anywhere anymore
    assert _constructs_async_openai(ALLOWED_FILE)
