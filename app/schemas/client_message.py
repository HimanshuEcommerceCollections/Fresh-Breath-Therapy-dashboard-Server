import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.base import ORMBase


class ClientMessageCreate(BaseModel):
    client_id: uuid.UUID
    body: str


class ClientMessageResponse(ORMBase):
    id: uuid.UUID
    client_id: uuid.UUID
    body: str
    created_at: datetime