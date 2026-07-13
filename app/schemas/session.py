import uuid
from datetime import datetime, date, time
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import SessionType, SessionStatus


class SessionCreate(BaseModel):
    client_id: uuid.UUID
    therapist_id: uuid.UUID
    date: date
    time: time
    type: SessionType
    status: SessionStatus = SessionStatus.SCHEDULED


class SessionUpdate(BaseModel):
    date: date | None = None
    time: time | None = None
    type: SessionType | None = None
    status: SessionStatus | None = None


class SessionResponse(ORMBase):
    id: uuid.UUID
    date: date
    time: time
    type: SessionType
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    client_id: uuid.UUID
    therapist_id: uuid.UUID