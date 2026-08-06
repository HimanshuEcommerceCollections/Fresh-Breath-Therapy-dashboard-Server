import uuid
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.schemas.fields import ShortName


class PackageBase(BaseModel):
    name: ShortName
    price: Decimal
    is_active: bool = True


class PackageCreate(PackageBase):
    pass


class PackageUpdate(BaseModel):
    name: ShortName | None = None
    price: Decimal | None = None
    is_active: bool | None = None


class PackageResponse(PackageBase, ORMBase):
    id: uuid.UUID