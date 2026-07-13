import uuid
from datetime import datetime, date
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.schemas.package import PackageResponse
from app.models.enums import PaymentMethod, PaymentStatus


class PaymentCreate(BaseModel):
    client_id: uuid.UUID
    package_id: uuid.UUID
    due: Decimal
    paid: Decimal = Decimal("0")
    method: PaymentMethod
    date: date
    status: PaymentStatus = PaymentStatus.PENDING


class PaymentUpdate(BaseModel):
    paid: Decimal | None = None
    method: PaymentMethod | None = None
    status: PaymentStatus | None = None


class PaymentResponse(ORMBase):
    id: uuid.UUID
    due: Decimal
    paid: Decimal
    method: PaymentMethod
    date: date
    status: PaymentStatus
    created_at: datetime
    client_id: uuid.UUID
    package: PackageResponse
    balance: Decimal = Decimal("0")