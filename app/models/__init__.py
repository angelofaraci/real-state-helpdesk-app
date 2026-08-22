"""SQLAlchemy ORM models package.

Importing this package registers every model on `Base.metadata`, which is
required before Alembic (or any other code) inspects `Base.metadata.tables`.
"""

from app.models.base import Base
from app.models.category import Category
from app.models.chat_message import ChatMessage
from app.models.chat_session import ChatSession
from app.models.classification import Classification
from app.models.contract import Contract
from app.models.daily_metric import DailyMetric
from app.models.invite_token import InviteToken
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.message import Message
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.property import Property
from app.models.refresh_token import RefreshToken
from app.models.sla_event import SlaEvent
from app.models.ticket import Ticket
from app.models.ticket_embedding import TicketEmbedding
from app.models.urgency_level import UrgencyLevel
from app.models.user import User

__all__ = [
    "Base",
    "Category",
    "ChatMessage",
    "ChatSession",
    "Classification",
    "Contract",
    "DailyMetric",
    "InviteToken",
    "KnowledgeBase",
    "KnowledgeChunk",
    "Message",
    "Notification",
    "Organization",
    "Property",
    "RefreshToken",
    "SlaEvent",
    "Ticket",
    "TicketEmbedding",
    "UrgencyLevel",
    "User",
]
