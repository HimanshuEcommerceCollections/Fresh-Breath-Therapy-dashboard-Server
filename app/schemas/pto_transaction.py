import uuid
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel, Field
from app.schemas.base import ORMBase
from app.models.enums import PtoTransactionType

# A single leave entry. Wide enough for a long stretch of leave booked in one
# go, narrow enough that a fat-fingered "800" instead of "8.00" is rejected
# rather than silently wiping out a therapist's balance.
MAX_USAGE_HOURS = Decimal("500")


class PtoUsageCreate(BaseModel):
    therapist_id: uuid.UUID
    # gt=0 matters: the balance check in record_pto_usage is `hours >
    # current_balance`, which a negative value passes trivially — and since
    # balance is accrued-minus-used, a negative USAGE row would *raise* the
    # balance. That's a way to mint PTO from nothing, so it's rejected here.
    hours: Decimal = Field(gt=0, le=MAX_USAGE_HOURS)
    date: date
    reason: str | None = Field(default=None, max_length=500)


class PtoTransactionResponse(ORMBase):
    id: uuid.UUID
    therapist_id: uuid.UUID
    type: PtoTransactionType
    hours: Decimal
    date: date | None
    reason: str | None
    created_at: datetime