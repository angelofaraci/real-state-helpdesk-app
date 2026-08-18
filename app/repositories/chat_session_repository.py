"""`ChatSessionRepository` — direct-scoped (`ChatSession` owns its own
`organization_id` column, so `scope_path` stays empty), same pattern as
`TicketRepository`/`PropertyRepository` (stage 4 — chatbot).
"""

from __future__ import annotations

from app.models.chat_session import ChatSession
from app.repositories.base import ScopedRepository


class ChatSessionRepository(ScopedRepository[ChatSession]):
    model = ChatSession
