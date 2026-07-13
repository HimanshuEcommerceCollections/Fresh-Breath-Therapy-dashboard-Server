import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import PtoTransactionType


class PtoTransactionCreate(BaseModel):
    therapist_id: uuid.UUID
    type: PtoTransactionType
    hours: Decimal
    rate_applied: Decimal | None = None
    source_session_id: uuid.UUID | None = None


class PtoTransactionResponse(ORMBase):
    id: uuid.UUID
    therapist_id: uuid.UUID
    type: PtoTransactionType
    hours: Decimal
    rate_applied: Decimal | None
    source_session_id: uuid.UUID | None
    created_at: datetime