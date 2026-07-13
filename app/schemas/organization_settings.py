import uuid
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase


class OrganizationSettingsUpdate(BaseModel):
    org_name: str | None = None
    primary_email: EmailStr | None = None
    timezone: str | None = None


class OrganizationSettingsResponse(ORMBase):
    id: uuid.UUID
    org_name: str
    primary_email: str
    timezone: str