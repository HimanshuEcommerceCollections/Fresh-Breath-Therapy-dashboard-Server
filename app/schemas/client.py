import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import ClientStatus


class ClientCreate(BaseModel):
    name: str
    email: EmailStr
    therapist_id: uuid.UUID
    location_id: uuid.UUID
    status: ClientStatus = ClientStatus.CONSULTATION_COMPLETED


class ClientUpdate(BaseModel):
    name: str | None = None
    email: EmailStr | None = None
    therapist_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    status: ClientStatus | None = None


class ClientResponse(ORMBase):
    id: uuid.UUID
    name: str
    email: str
    status: ClientStatus
    created_at: datetime
    location: LocationResponse
    therapist: TherapistResponse
    lifetime_value: Decimal = Decimal("0")
    sessions_count: int = 0