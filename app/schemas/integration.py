import uuid
from datetime import datetime
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import IntegrationStatus


class IntegrationConnect(BaseModel):
    credentials: dict | None = None


class IntegrationResponse(ORMBase):
    id: uuid.UUID
    name: str
    status: IntegrationStatus
    connected_at: datetime | None