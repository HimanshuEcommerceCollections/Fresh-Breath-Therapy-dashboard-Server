import uuid
from datetime import datetime, date
from enum import Enum
from pydantic import BaseModel
from app.schemas.base import ORMBase


class FollowUpStatus(str, Enum):
    PENDING = "pending"
    OVERDUE = "overdue"
    COMPLETED = "completed"


class FollowUpCreate(BaseModel):
    client_id: uuid.UUID
    due_date: date
    notes: str | None = None
    reminder: bool = False


class FollowUpUpdate(BaseModel):
    due_date: date | None = None
    notes: str | None = None
    reminder: bool | None = None
    completed_at: datetime | None = None


class FollowUpResponse(ORMBase):
    id: uuid.UUID
    client_id: uuid.UUID
    due_date: date
    notes: str | None
    reminder: bool
    completed_at: datetime | None
    created_at: datetime
    status: FollowUpStatus = FollowUpStatus.PENDING

class FollowUpStats(BaseModel):
    pending: int
    overdue: int
    completed: int