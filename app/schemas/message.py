"""Pydantic request/response schemas for the ticket-messaging endpoints.

`MessageCreate` deliberately has no `author_type` field: it is never
client-settable, only derived server-side from the caller's role (see
`app.services.message_service.create_message`). There is no code path
anywhere that lets a human caller set `author_type=bot` — it isn't even an
accepted field on this schema, so a client attempting to inject one is
silently dropped by Pydantic's default `extra="ignore"` behavior before the
service layer ever sees it. This is enforced structurally by the schema's
shape, not by a runtime check.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, field_validator

from app.models.enums import AuthorType


class MessageCreate(BaseModel):
    content: str

    @field_validator("content")
    @classmethod
    def content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class MessageResponse(BaseModel):
    id: UUID
    ticket_id: UUID
    author_type: AuthorType
    content: str
    is_ai_suggestion: bool
    created_at: datetime

    model_config = {"from_attributes": True}
