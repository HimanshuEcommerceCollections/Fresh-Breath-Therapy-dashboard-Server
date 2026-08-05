import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.schemas.package import PackageResponse
from app.models.enums import EnrollmentStatus


class ClientBrief(BaseModel):
    id: uuid.UUID
    name: str


class EnrollmentResponse(ORMBase):
    id: uuid.UUID
    client_id: uuid.UUID
    package_id: uuid.UUID
    package_price_snapshot: Decimal
    total_paid: Decimal
    amount_due: Decimal
    status: EnrollmentStatus
    started_at: datetime
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EnrollmentWithDetailsResponse(EnrollmentResponse):
    client: ClientBrief
    package: PackageResponse
