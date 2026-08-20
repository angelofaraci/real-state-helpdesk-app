"""`ChatMessageRepository` — join-scoped through `ChatSession`
(`ChatMessage` has no `organization_id` column of its own; scoping goes
through `chat_sessions.organization_id`), same join-path pattern as
`ContractRepository` (stage 4 — chatbot).

Unlike `MessageRepository` (join-scoped through `Ticket`, but deliberately
NOT relied upon alone for authorization because ticket visibility is
role-fused), a chat session has no equivalent role-based narrowing beyond
plain organization membership — a chat session is only ever visible within
its own org — so this repository's `scope_clause()` is a complete
authorization boundary on its own, no service-layer pre-check required.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Select

from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.repositories.base import ScopedRepository


class ChatMessageRepository(ScopedRepository[ChatMessage]):
    model = ChatMessage
    scope_path = ((ChatSession, "chat_session_id"),)

    def list_for_session(self, chat_session_id: UUID) -> Select[tuple[ChatMessage]]:
        """A scoped `SELECT` of every message in `chat_session_id`, in
        chronological order (`created_at` ascending, `id` as tiebreak for
        same-timestamp inserts — mirrors the
        `ix_chat_messages_chat_session_id_created_at_id` index)."""
        return (
            self.select()
            .where(ChatMessage.chat_session_id == chat_session_id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
