import uuid
from datetime import datetime, date
from enum import Enum
from pydantic import BaseModel, Field
from app.schemas.base import ORMBase


# Follow-up notes are a one-line reminder ("call re: rescheduling"), not a
# clinical record — the table renders them on a single truncated line, so
# anything longer was already invisible in the UI. Defined once and used by
# both create and update so the two can't diverge.
MAX_NOTES_LENGTH = 40


class FollowUpStatus(str, Enum):
    PENDING = "pending"
    OVERDUE = "overdue"
    COMPLETED = "completed"


class FollowUpCreate(BaseModel):
    client_id: uuid.UUID
    due_date: date
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
    reminder: bool = False


class FollowUpUpdate(BaseModel):
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)
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