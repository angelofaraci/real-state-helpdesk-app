"""ChatMessage model — a single turn in a `ChatSession` (stage 4 — chatbot).

Three roles: `user` (visitor input), `assistant` (LLM output, possibly
tool-call-only with blank content), and `tool` (a tool result fed back to
the LLM, which must carry the `tool_name` it came from).
"""

import uuid

from sqlalchemy import CheckConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ChatMessageRole


class ChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_messages"

    chat_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[ChatMessageRole] = mapped_column(
        SAEnum(
            ChatMessageRole,
            name="chat_message_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    # Nullable-in-practice via the CHECK constraint below: an assistant
    # message may be blank when it is a tool-call-only turn.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Set only when role='tool' — the name of the tool that produced this
    # message's content.
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Correlates a tool result back to the assistant tool-call that
    # requested it. Nullable: irrelevant for plain user/assistant turns.
    tool_call_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(role = 'tool') = (tool_name IS NOT NULL)",
            name="ck_chat_messages_tool_name_iff_role_tool",
        ),
        CheckConstraint(
            "role = 'assistant' OR btrim(content) <> ''",
            name="ck_chat_messages_content_not_blank_unless_assistant",
        ),
        Index(
            "ix_chat_messages_chat_session_id_created_at_id",
            "chat_session_id",
            "created_at",
            "id",
        ),
    )
