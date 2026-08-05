import uuid
from datetime import datetime, date
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.schemas.package import PackageResponse
from app.schemas.enrollment import EnrollmentResponse
from app.models.enums import PaymentMethod


class PaymentCreate(BaseModel):
    client_id: uuid.UUID
    package_id: uuid.UUID
    amount_paid: Decimal
    method: PaymentMethod
    date: date


class PaymentUpdate(BaseModel):
    # The ledger row itself is immutable (amount_paid/date/enrollment can't
    # be edited without corrupting the running total_paid/balance_after
    # history) — only a data-entry typo on the method is correctable here.
    method: PaymentMethod | None = None


class PaymentResponse(ORMBase):
    id: uuid.UUID
    enrollment_id: uuid.UUID
    client_id: uuid.UUID
    amount_paid: Decimal
    balance_after: Decimal
    method: PaymentMethod
    date: date
    created_at: datetime
    package: PackageResponse
    enrollment: EnrollmentResponse


class PaymentCreateResult(BaseModel):
    payment: PaymentResponse
    enrollment: EnrollmentResponse
    is_new_cycle: bool
