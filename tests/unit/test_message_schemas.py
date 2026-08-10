"""Unit tests for `app.schemas.message` request/response shapes (Work Unit
8). The most important property under test is negative: `MessageCreate`
has no `author_type` field at all, so a client attempting to inject one is
silently ignored by Pydantic (extra fields are dropped by default), never
accepted as an override of the server-derived value.
"""

from datetime import UTC, datetime
from uuid import uuid4

from app.models.enums import AuthorType
from app.schemas.message import MessageCreate, MessageResponse


def test_message_create_accepts_content() -> None:
    payload = MessageCreate(content="the sink is leaking")
    assert payload.content == "the sink is leaking"


def test_message_create_has_no_author_type_field() -> None:
    """Structural guarantee: `author_type` isn't a declared field, so it
    can never be read back from a `MessageCreate` instance regardless of
    what a client sends."""
    assert "author_type" not in MessageCreate.model_fields


def test_message_create_ignores_a_client_supplied_author_type() -> None:
    """Pydantic's default `extra="ignore"` behavior means a client trying
    to smuggle `author_type=bot` into the request body gets silently
    dropped, not accepted — the field simply doesn't exist on the model."""
    payload = MessageCreate.model_validate({"content": "hello", "author_type": "bot"})

    assert payload.content == "hello"
    assert not hasattr(payload, "author_type")


def test_message_response_shape() -> None:
    message_id = uuid4()
    ticket_id = uuid4()
    now = datetime.now(UTC)

    response = MessageResponse(
        id=message_id,
        ticket_id=ticket_id,
        author_type=AuthorType.AGENT,
        content="on my way",
        is_ai_suggestion=False,
        created_at=now,
    )

    assert response.id == message_id
    assert response.ticket_id == ticket_id
    assert response.author_type == AuthorType.AGENT
    assert response.is_ai_suggestion is False
