"""Python enums mirroring the native Postgres enum types.

Each enum's `value`s must match the labels created for its corresponding
Postgres enum type in `app/alembic/versions/0001_initial.py` exactly.
"""

from enum import Enum


class UserRole(str, Enum):
    """Maps to the Postgres enum type `user_role`."""

    TENANT = "tenant"
    OWNER = "owner"
    AGENT = "agent"
    ADMIN = "admin"


class UserStatus(str, Enum):
    """Maps to the Postgres enum type `user_status`."""

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class PropertyType(str, Enum):
    """Maps to the Postgres enum type `property_type`."""

    APARTMENT = "apartment"
    HOUSE = "house"
    COMMERCIAL = "commercial"
    OTHER = "other"


class ContractStatus(str, Enum):
    """Maps to the Postgres enum type `contract_status`."""

    ACTIVE = "active"
    EXPIRED = "expired"
    TERMINATED = "terminated"


class TicketChannel(str, Enum):
    """Maps to the Postgres enum type `ticket_channel`."""

    WEB = "web"
    EMAIL = "email"
    WHATSAPP = "whatsapp"


class TicketStatus(str, Enum):
    """Maps to the Postgres enum type `ticket_status`."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_ON_CUSTOMER = "waiting_on_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"
    # Added by migration 0006 (stage 4 — chatbot): a ticket created by the
    # chatbot when it hands a conversation off to a human agent.
    ESCALATED = "escalated"


class ChatSessionStatus(str, Enum):
    """Maps to the Postgres enum type `chat_session_status`."""

    ACTIVE = "active"
    ESCALATED = "escalated"
    CLOSED = "closed"


class ChatMessageRole(str, Enum):
    """Maps to the Postgres enum type `chat_message_role`."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AuthorType(str, Enum):
    """Maps to the Postgres enum type `author_type`."""

    USER = "user"
    AGENT = "agent"
    BOT = "bot"


class ClassificationStatus(str, Enum):
    """Maps to the Postgres enum type `classification_status`."""

    PENDING = "pending"
    CLASSIFIED = "classified"
