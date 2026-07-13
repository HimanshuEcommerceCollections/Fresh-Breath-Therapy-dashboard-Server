import uuid
from pydantic import BaseModel
from app.schemas.base import ORMBase


class RoleResponse(ORMBase):
    id: uuid.UUID
    name: str
    permissions: dict