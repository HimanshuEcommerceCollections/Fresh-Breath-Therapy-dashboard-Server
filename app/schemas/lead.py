import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import LeadStatus


class LeadCreate(BaseModel):
    name: str
    age: int | None = None
    gender_or_pronoun: str | None = None
    email: EmailStr
    phone: str
    location_id: uuid.UUID
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    status: LeadStatus = LeadStatus.NEW_LEAD


class LeadUpdate(BaseModel):
    name: str | None = None
    age: int | None = None
    gender_or_pronoun: str | None = None
    email: EmailStr | None = None
    phone: str | None = None
    location_id: uuid.UUID | None = None
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    status: LeadStatus | None = None


class LeadResponse(ORMBase):
    id: uuid.UUID
    name: str
    age: int | None
    gender_or_pronoun: str | None
    email: str
    phone: str
    source: str | None
    status: LeadStatus
    converted_client_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    location: LocationResponse
    therapist: TherapistResponse | None