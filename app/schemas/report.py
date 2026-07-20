import uuid
from decimal import Decimal
from pydantic import BaseModel


class SalesPoint(BaseModel):
    month: str
    total: Decimal


class ClientStatusPoint(BaseModel):
    status: str
    count: int


class TeamPerformancePoint(BaseModel):
    therapist_id: uuid.UUID
    therapist_name: str
    sessions: int


class ConversionStage(BaseModel):
    status: str
    count: int
    percent: float


class ConversionReport(BaseModel):
    overall_rate: float
    total_leads: int
    stages: list[ConversionStage]


class UtilizationPoint(BaseModel):
    therapist_id: uuid.UUID
    therapist_name: str
    utilization: float


class RevenuePoint(BaseModel):
    therapist_id: uuid.UUID
    therapist_name: str
    revenue: Decimal


class RetentionPoint(BaseModel):
    location_id: uuid.UUID
    location_name: str
    retention_months: float