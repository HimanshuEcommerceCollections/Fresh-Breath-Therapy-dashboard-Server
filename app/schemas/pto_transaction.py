import uuid
from datetime import date, datetime
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import PtoTransactionType


class PtoUsageCreate(BaseModel):
    therapist_id: uuid.UUID
    hours: Decimal
    date: date
    reason: str | None = None


class PtoTransactionResponse(ORMBase):
    id: uuid.UUID
    therapist_id: uuid.UUID
    type: PtoTransactionType
    hours: Decimal
    date: date | None
    reason: str | None
    created_at: datetime