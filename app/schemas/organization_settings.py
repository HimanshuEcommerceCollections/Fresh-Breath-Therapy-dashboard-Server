import uuid
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.fields import PersonName, Email


class OrganizationSettingsUpdate(BaseModel):
    org_name: PersonName | None = None
    primary_email: Email | None = None
    timezone: str | None = None


class OrganizationSettingsResponse(ORMBase):
    id: uuid.UUID
    org_name: str
    primary_email: str
    timezone: str