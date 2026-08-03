import re
import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import LeadStatus

PHONE_PATTERN = re.compile(r"^[0-9+\-()\s]{7,20}$")


def _validate_phone(v: str | None) -> str | None:
    if v is not None and not PHONE_PATTERN.match(v):
        raise ValueError(
            "Phone number must be 7-20 characters, using only digits, spaces, +, -, or parentheses"
        )
    return v


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: EmailStr
    phone: str = Field(min_length=7, max_length=20)
    location_id: uuid.UUID
    therapist_id: uuid.UUID | None = None
    source: str | None = None
    status: LeadStatus = LeadStatus.NEW_LEAD

    _validate_phone = field_validator("phone")(_validate_phone)


class LeadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    age: int | None = Field(default=None, ge=0, le=120)
    gender_or_pronoun: str | None = None
    email: EmailStr | None = None
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
    status: LeadStatus
    converted_client_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    location: LocationResponse
    therapist: TherapistResponse | None
