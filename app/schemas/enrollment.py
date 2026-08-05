import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field
from app.schemas.base import ORMBase
from app.schemas.package import PackageResponse
from app.models.enums import EnrollmentStatus, PaymentStatus


class ClientBrief(ORMBase):
    """ORMBase, not BaseModel — this gets validated straight off a SQLAlchemy
    Client instance (see schemas/session.py's ClientBrief). Without
    from_attributes, any code path that validates the parent explicitly rather
    than through FastAPI's response_model blows up on this nested field."""
    id: uuid.UUID
    name: str


class EnrollmentCreate(BaseModel):
    """Assign a package to a client without taking money yet — the invoice
    starts at total_paid 0, which renders as PENDING. Recording payments
    against it later walks it to PARTIALLY_PAID and then PAID."""
    client_id: uuid.UUID
    package_id: uuid.UUID


class EnrollmentStatusUpdate(BaseModel):
    """Admin sets the displayed payment status. Only OVERDUE is actually
    stored; picking any of the other three clears the overdue flag and lets
    the status re-derive from the ledger (so it can't contradict the money)."""
    status: PaymentStatus


class EnrollmentResponse(ORMBase):
    id: uuid.UUID
    client_id: uuid.UUID
    package_id: uuid.UUID
    package_price_snapshot: Decimal
    total_paid: Decimal
    amount_due: Decimal
    status: EnrollmentStatus
    is_overdue: bool = False
    # Derived on the model (Enrollment.payment_status), not stored.
    payment_status: PaymentStatus = Field(default=PaymentStatus.PENDING)
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EnrollmentWithDetailsResponse(EnrollmentResponse):
    client: ClientBrief
    package: PackageResponse
