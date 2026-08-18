"""Pydantic request/response schemas for the chat-widget API (stage 4 —
chatbot, PR5 — API wiring)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.enums import ChatMessageRole, ChatSessionStatus


class ChatSessionCreateRequest(BaseModel):
    widget_key: str


class ChatSessionCreateResponse(BaseModel):
    id: UUID
    status: ChatSessionStatus
    is_anonymous: bool
    chat_token: str
    expires_at: datetime


class ChatMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class ChatMessageResponse(BaseModel):
    reply: str
    session_status: ChatSessionStatus
    escalated: bool
    ticket_id: UUID | None
    tools_used: list[str]


class ChatMessageHistoryItem(BaseModel):
    id: UUID
    role: ChatMessageRole
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}
