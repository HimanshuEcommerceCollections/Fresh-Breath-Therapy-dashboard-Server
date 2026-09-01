import uuid
# `date as date_type`, matching schemas/session.py: a field named `date`
# shadows the imported name inside the class body, so `date | None` in a
# later field resolves to None and raises at import.
from datetime import datetime, date as date_type
from decimal import Decimal
from pydantic import BaseModel, Field
from app.schemas.base import ORMBase
from app.models.enums import PaymentMethod, PaymentStatus


class ClientBrief(ORMBase):
    id: uuid.UUID
    name: str


class PaymentCreate(BaseModel):
    client_id: uuid.UUID
    # gt=0: a payment for nothing is a data-entry slip, and a zero-amount
    # PENDING row would sit in the outstanding figure forever contributing
    # nothing. A session that is not being billed is CANCELLED, not zero.
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    method: PaymentMethod
    status: PaymentStatus = PaymentStatus.PENDING
    date: date_type


class PaymentUpdate(BaseModel):
    """Every field is editable.

    The old ledger row was immutable except for its method, because amount and
    date fed a running balance that would silently go wrong if either changed.
    There is no running balance any more, so a mistyped amount is safe to fix -
    and it had otherwise been permanent.
    """
    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    method: PaymentMethod | None = None
    status: PaymentStatus | None = None
    date: date_type | None = None


class PaymentResponse(ORMBase):
    id: uuid.UUID
    client_id: uuid.UUID
    amount: Decimal
    method: PaymentMethod
    status: PaymentStatus
    date: date_type
    created_at: datetime
    updated_at: datetime
    client: ClientBrief
