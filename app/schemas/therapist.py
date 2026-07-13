import uuid
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.location import LocationResponse


class TherapistCreate(BaseModel):
    name: str
    credential: str | None = None
    location_id: uuid.UUID
    email: EmailStr
    avatar_url: str | None = None


class TherapistUpdate(BaseModel):
    name: str | None = None
    credential: str | None = None
    location_id: uuid.UUID | None = None
    email: EmailStr | None = None
    avatar_url: str | None = None
    is_active: bool | None = None


class TherapistResponse(ORMBase):
    id: uuid.UUID
    name: str
    credential: str | None
    email: str
    avatar_url: str | None
    is_active: bool
    location: LocationResponse