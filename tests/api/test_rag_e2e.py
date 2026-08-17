"""End-to-end test for the Stage 3 RAG flow (Work Unit 6.8): a knowledge
base document is chunked (via the REAL `app.services.chunking.chunk_document`
— the actual document chunker, not a stub), a ticket on the same topic
requests `GET /tickets/{ticket_id}/suggested-response`, and the response
must carry a suggestion that cites at least one retrieved chunk.

No live Postgres is available in this sandbox (see every other PR's test
suite for the same constraint), so this drives the FULL real chain —
`app.api.v1.tickets.get_suggested_response` -> `app.services.rag
.suggest_response` -> `KnowledgeChunkRepository.search`/
`TicketEmbeddingRepository.search` — through the actual FastAPI route and
the actual (non-monkeypatched) `rag.suggest_response` service function,
with only the `AsyncSession` faked at the `session.execute` boundary
(scripted results, one per query, in call order) and the embedder/LLM
faked via `app.dependency_overrides` — exactly the technique
`tests/unit/test_worker_rag.py` uses for the ingestion worker and
`tests/unit/test_rag_service.py` uses for `suggest_response` directly.
This proves the wiring end-to-end (real chunker -> real retrieval SQL ->
real prompt assembly -> real response schema), not just each piece in
isolation.

The LLM's response is mocked to explicitly cite "Knowledge Base Article 1"
(matching `app.services.rag._build_suggestion_prompt`'s own citation
convention) so the "cites >= 1 retrieved chunk" assertion is deterministic,
never dependent on real LLM output.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps, deps_rag
from app.core.session import get_session
from app.main import app
from app.models.enums import TicketChannel, TicketStatus, UserRole, UserStatus
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.ticket import Ticket
from app.services.chunking import chunk_document

_UNSET = object()


def _fake_principal(role=UserRole.AGENT, organization_id=_UNSET):
    return type(
        "FakeUser",
        (),
        {
            "id": uuid4(),
            "organization_id": uuid4() if organization_id is _UNSET else organization_id,
            "name": "Some Agent",
            "email": "agent@example.com",
            "role": role,
            "status": UserStatus.ACTIVE,
        },
    )()


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1] * 768 for _ in texts]


class _FakeLLMClient:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

        class _Completions:
            def __init__(self, outer: "_FakeLLMClient") -> None:
                self._outer = outer

            async def create(self, **kwargs):
                self._outer.calls.append(kwargs)
                message = MagicMock(content=self._outer._content)
                choice = MagicMock(message=message)
                return MagicMock(choices=[choice])

        self.chat = MagicMock(completions=_Completions(self))


def _execute_result(*, scalar=None, scalars_list=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    if scalars_list is not None:
        result.scalars.return_value.all.return_value = scalars_list
    return result


def _rows_result(rows: list[tuple[object, float]]) -> MagicMock:
    result = MagicMock()
    result.all.return_value = rows
    return result


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_kb_doc_chunked_and_embedded_grounds_a_suggestion_citing_a_retrieved_chunk(
    client, monkeypatch
) -> None:
    organization_id = uuid4()
    principal = _fake_principal(role=UserRole.AGENT, organization_id=organization_id)
    ticket_id = uuid4()

    ticket = Ticket(
        id=ticket_id,
        organization_id=organization_id,
        user_id=uuid4(),
        property_id=None,
        contract_id=None,
        title="Leaking kitchen faucet",
        description="The kitchen faucet has been dripping steadily for a week.",
        channel=TicketChannel.WEB,
        status=TicketStatus.OPEN,
        agent_id=None,
        sla_due_at=None,
        closed_at=None,
    )

    # 1. Real document chunker (not a stub) processes a knowledge base
    # article on the SAME topic as the ticket ("leaking faucet").
    kb_document_text = (
        "Plumbing maintenance policy.\n\n"
        "A leaking or dripping faucet is covered under standard maintenance. "
        "Tenants should report a leaking faucet as soon as it is noticed. "
        "The most common fix for a dripping faucet is replacing the internal "
        "washer, which a maintenance technician can do during a routine visit."
    )
    chunks = chunk_document(kb_document_text)
    assert chunks, "the chunker must actually produce chunks from real text"

    knowledge_chunk = KnowledgeChunk(
        id=uuid4(),
        knowledge_base_id=uuid4(),
        organization_id=organization_id,
        chunk_index=chunks[0].index,
        content=chunks[0].content,
        token_count=10,
        embedding=[0.1] * 768,
    )

    # 2. Script the exact `session.execute` call sequence
    # `rag.suggest_response` issues for a cache-miss regeneration (see
    # `tests/unit/test_rag_service.py`'s identical sequence).
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _execute_result(scalar=ticket),  # unlocked get_or_404
            _execute_result(scalar=None),  # _latest_suggestion (unlocked)
            _execute_result(scalar=ticket),  # locked get_or_404
            _execute_result(scalar=None),  # _latest_suggestion (re-check)
            _execute_result(scalars_list=[]),  # thread messages
            _rows_result([(knowledge_chunk, 0.81)]),  # chunk search
            _rows_result([]),  # ticket-embedding search
        ]
    )
    session.commit = AsyncMock()

    # `MessageRepository.add()` constructs a real `Message` ORM instance
    # and calls `session.add(instance)` — with a real session/flush, the
    # `id` column's Python-side `default=uuid.uuid4` (see
    # `app.models.base.UUIDPrimaryKeyMixin`) is populated at flush time.
    # `session` here is fully mocked (no real flush), so simulate that
    # exact default-on-flush behavior for the new BOT suggestion message.
    def _assign_id_like_a_real_flush_would(instance) -> None:
        if getattr(instance, "id", None) is None:
            instance.id = uuid4()

    session.add = MagicMock(side_effect=_assign_id_like_a_real_flush_would)

    embedder = _FakeEmbedder()
    llm_client = _FakeLLMClient(
        content=(
            "Per Knowledge Base Article 1, this is a known issue — a "
            "technician will replace the washer to fix the drip."
        )
    )

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[deps.get_principal] = lambda: principal
    app.dependency_overrides[deps_rag.get_rag_embedder] = lambda: embedder
    app.dependency_overrides[deps_rag.get_llm_client] = lambda: llm_client

    # 3. Hit the real endpoint end-to-end.
    response = client.get(f"/api/v1/tickets/{ticket_id}/suggested-response")

    assert response.status_code == 200
    body = response.json()
    assert body["reason"] is None
    assert body["suggestion"] is not None
    assert body["suggestion"]["top_similarity"] == pytest.approx(0.81)
    # The suggestion cites the retrieved chunk (deterministic, from the
    # mocked LLM response — never dependent on real LLM output).
    assert "Knowledge Base Article 1" in body["suggestion"]["content"]

    # Retrieval actually ran through the real chunker's output, and the
    # prompt assembled by `rag._build_suggestion_prompt` embedded that
    # real chunk content.
    assert embedder.calls
    prompt = llm_client.calls[0]["messages"][0]["content"]
    assert chunks[0].content in prompt
    session.commit.assert_awaited_once()
