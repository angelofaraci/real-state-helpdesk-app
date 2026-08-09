"""SQLAlchemy ORM models package.

Importing this package registers every model on `Base.metadata`, which is
required before Alembic (or any other code) inspects `Base.metadata.tables`.
"""

from app.models.base import Base
from app.models.category import Category
from app.models.classification import Classification
from app.models.contract import Contract
from app.models.invite_token import InviteToken
from app.models.message import Message
from app.models.organization import Organization
from app.models.property import Property
from app.models.refresh_token import RefreshToken
from app.models.ticket import Ticket
from app.models.urgency_level import UrgencyLevel
from app.models.user import User

__all__ = [
    "Base",
    "Category",
    "Classification",
    "Contract",
    "InviteToken",
    "Message",
    "Organization",
    "Property",
    "RefreshToken",
    "Ticket",
    "UrgencyLevel",
    "User",
]
