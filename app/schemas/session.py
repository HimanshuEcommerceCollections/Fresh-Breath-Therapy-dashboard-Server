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
    # Reassignment. A session booked against the wrong clinician or the wrong
    # client was previously only fixable by deleting and re-creating it, which
    # loses the record's history and its id.
    therapist_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None


class SessionSearchRequest(BaseModel):
    therapist_ids: list[uuid.UUID] | None = None
    client_id: uuid.UUID | None = None
    status: SessionStatus | None = None
    date_from: date_type | None = None
    date_to: date_type | None = None
    # Free text matched against the CLIENT's and the THERAPIST's name. The
    # admin looking for a session knows one of those two names, not a date
    # range or an id.
    search: str | None = None
    cursor: str | None = None
    limit: int = 25


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