import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import LeadStatus
# The phone rule moved to fields.py when clients gained a phone column too —
# one definition, so the two entry points can't drift apart.
from app.schemas.fields import PersonName, Email, validate_phone as _validate_phone


class LeadCreate(BaseModel):
    name: PersonName
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: Email
    phone: str = Field(min_length=7, max_length=20)
    location_id: uuid.UUID
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    status: LeadStatus = LeadStatus.NEW_LEAD

    _validate_phone = field_validator("phone")(_validate_phone)


class LeadUpdate(BaseModel):
    name: PersonName | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: Email | None = None
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    location_id: uuid.UUID | None = None
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    status: LeadStatus | None = None

    _validate_phone = field_validator("phone")(_validate_phone)


class LeadResponse(ORMBase):
    id: uuid.UUID
    name: str
    age: int | None
    gender_or_pronoun: str | None
    email: str
    phone: str
    source: str | None
    # Captured by the public website form and delivered via the lead webhook.
    message: str | None = None
    preferred_datetime: str | None = None
    consent_given: bool = False
    # The automation's own tracking fields — always None for leads added any
    # other way (LeadCreate/LeadUpdate don't accept them).
    customer_id: str | None = None
    payment_status: str | None = None
    visit_status: str | None = None
    status: LeadStatus
    converted_client_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    location: LocationResponse
    therapist: TherapistResponse | None
