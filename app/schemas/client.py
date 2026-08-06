import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse
from app.schemas.therapist import TherapistResponse
from app.models.enums import ClientStatus
from app.schemas.fields import PersonName, Email


class ClientCreate(BaseModel):
    name: PersonName
    email: Email
    therapist_id: uuid.UUID
    location_id: uuid.UUID
    status: ClientStatus = ClientStatus.CONSULTATION_COMPLETED


class ClientUpdate(BaseModel):
    name: PersonName | None = None
    email: Email | None = None
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