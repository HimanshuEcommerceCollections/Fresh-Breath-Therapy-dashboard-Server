import uuid
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase


class PackageBase(BaseModel):
    name: str
    price: Decimal
    is_active: bool = True


class PackageCreate(PackageBase):
    pass


class PackageUpdate(BaseModel):
    name: str | None = None
    price: Decimal | None = None
    is_active: bool | None = None


class PackageResponse(PackageBase, ORMBase):
    id: uuid.UUID