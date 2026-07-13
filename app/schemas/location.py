import uuid
from pydantic import BaseModel
from app.schemas.base import ORMBase


class LocationCreate(BaseModel):
    name: str


class LocationResponse(ORMBase):
    id: uuid.UUID
    name: str