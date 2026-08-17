"""Pydantic request/response schemas for the `/api/v1/knowledge-base`
endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_type: str | None = None

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class KnowledgeBasePatch(BaseModel):
    """All fields optional — only supplied fields are applied. See
    `app.services.knowledge_base_service.update_knowledge_base`: any
    supplied field resets `status` back to `"pending"` and clears
    `embedding_error` server-side, regardless of which field changed."""

    title: str | None = None
    content: str | None = None
    source_type: str | None = None

    @field_validator("title")
    @classmethod
    def _title_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value

    @field_validator("content")
    @classmethod
    def _content_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("content must not be blank")
        return value


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    organization_id: UUID
    title: str
    content: str
    source_type: str | None
    status: str
    embedding_error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
