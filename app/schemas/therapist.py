import uuid
from decimal import Decimal
from pydantic import BaseModel, EmailStr, Field
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.fields import PersonName, Email


class TherapistCreate(BaseModel):
    name: PersonName
    credential: str | None = Field(default=None, max_length=100)
    specialization: str | None = Field(default=None, max_length=200)
    employment_status: str | None = Field(default=None, max_length=50)
    location_id: uuid.UUID
    email: Email
    avatar_url: str | None = None
    # Paired with avatar_url so a file uploaded via /api/uploads/avatar
    # stays deletable once it is attached to a therapist.
    avatar_storage_key: str | None = None


class TherapistUpdate(BaseModel):
    name: PersonName | None = None
    credential: str | None = Field(default=None, max_length=100)
    specialization: str | None = Field(default=None, max_length=200)
    employment_status: str | None = Field(default=None, max_length=50)
    location_id: uuid.UUID | None = None
    email: Email | None = None
    avatar_url: str | None = None
    # Paired with avatar_url so a file uploaded via /api/uploads/avatar
    # stays deletable once it is attached to a therapist.
    avatar_storage_key: str | None = None
    is_active: bool | None = None


class TherapistResponse(ORMBase):
    id: uuid.UUID
    name: str
    credential: str | None
    specialization: str | None = None
    employment_status: str | None = None
    email: str
    avatar_url: str | None
    # avatar_storage_key is deliberately NOT exposed here. The client never
    # needs it, and on S3 it would be an object key — no use to anyone without
    # a signature, but no reason to publish either.
    is_active: bool
    location: LocationResponse
    active_client_count: int = 0
    revenue: Decimal = Decimal("0")
    ytd_sessions: int = 0
    pto_balance: Decimal = Decimal("0")
