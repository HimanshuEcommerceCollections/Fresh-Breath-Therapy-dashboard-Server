import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel

from app.schemas.base import ORMBase
from app.models.notification import NotificationCategory, NotificationBadge


class NotificationTab(str, Enum):
    ALL = "all"
    UNREAD = "unread"
    FOLLOW_UP_REMINDERS = "follow_up_reminders"
    ALERTS = "alerts"
    READ = "read"


class NotificationResponse(ORMBase):
    id: uuid.UUID
    category: NotificationCategory
    badge: NotificationBadge
    title: str
    body: str
    therapist_id: uuid.UUID | None
    related_entity_type: str | None
    related_entity_id: uuid.UUID | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationSummaryResponse(BaseModel):
    unread: int
    follow_up_reminders: int
    alerts: int