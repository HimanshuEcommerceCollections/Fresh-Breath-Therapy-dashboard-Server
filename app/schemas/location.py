import uuid
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.schemas.fields import ShortName


class LocationCreate(BaseModel):
    name: ShortName


class LocationResponse(ORMBase):
    id: uuid.UUID
    name: str