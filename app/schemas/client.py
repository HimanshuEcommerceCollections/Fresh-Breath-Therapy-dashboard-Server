import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, EmailStr, Field, field_validator
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import ClientStatus
from app.schemas.fields import (
    MAX_PHONE_LENGTH, MIN_PHONE_LENGTH, PersonName, Email, validate_phone,
)


class ClientCreate(BaseModel):
    name: PersonName
    email: Email
    # Optional, unlike a lead's: clients converted before this column existed
    # have no number on file, so requiring one would make every one of them
    # un-editable until someone tracked a phone number down.
    phone: str | None = Field(
        default=None, min_length=MIN_PHONE_LENGTH, max_length=MAX_PHONE_LENGTH
    )
    therapist_id: uuid.UUID
    location_id: uuid.UUID
    status: ClientStatus = ClientStatus.CONSULTATION_COMPLETED

    _validate_phone = field_validator("phone")(validate_phone)


class ClientUpdate(BaseModel):
    name: PersonName | None = None
    email: Email | None = None
    phone: str | None = Field(
        default=None, min_length=MIN_PHONE_LENGTH, max_length=MAX_PHONE_LENGTH
    )
    therapist_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    status: ClientStatus | None = None

    _validate_phone = field_validator("phone")(validate_phone)


class ClientResponse(ORMBase):
    id: uuid.UUID
    name: str
    email: str
    phone: str | None = None
    status: ClientStatus
    created_at: datetime
    location: LocationResponse
    therapist: TherapistResponse
    lifetime_value: Decimal = Decimal("0")
    sessions_count: int = 0