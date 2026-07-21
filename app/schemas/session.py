import uuid
from datetime import date as date_type, time as time_type, datetime
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import SessionType, SessionStatus

TERMINAL_STATUSES = {SessionStatus.COMPLETED, SessionStatus.CANCELLED, SessionStatus.NO_SHOW}


class SessionCreate(BaseModel):
    client_id: uuid.UUID
    therapist_id: uuid.UUID
    date: date_type
    time: time_type
    type: SessionType
    status: SessionStatus = SessionStatus.SCHEDULED


class SessionUpdate(BaseModel):
    date: date_type | None = None
    time: time_type | None = None
    type: SessionType | None = None
    status: SessionStatus | None = None


class SessionSearchRequest(BaseModel):
    therapist_ids: list[uuid.UUID] | None = None
    client_id: uuid.UUID | None = None
    status: SessionStatus | None = None
    date_from: date_type | None = None
    date_to: date_type | None = None


class ClientBrief(ORMBase):
    id: uuid.UUID
    name: str


class TherapistBrief(ORMBase):
    id: uuid.UUID
    name: str


class SessionResponse(ORMBase):
    id: uuid.UUID
    date: date_type
    time: time_type
    type: SessionType
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    client: ClientBrief
    therapist: TherapistBrief