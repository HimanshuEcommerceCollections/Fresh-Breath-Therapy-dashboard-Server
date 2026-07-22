from decimal import Decimal
from datetime import date
from pydantic import BaseModel


class LeadStat(BaseModel):
    total: int
    new_this_month: int


class ClientStat(BaseModel):
    active: int
    new_last_30_days: int


class SessionMetrics(BaseModel):
    total: int
    completed: int
    upcoming: int
    missed: int
    cancelled: int
    today: int


class RevenueMetrics(BaseModel):
    total_revenue: Decimal
    monthly_revenue: Decimal
    pending_payments: Decimal
    collected: Decimal
    avg_per_client: Decimal


class RevenueTrendPoint(BaseModel):
    month: str
    collected: Decimal
    pending: Decimal


class PaymentStatusCount(BaseModel):
    status: str
    count: int


class FunnelStage(BaseModel):
    status: str
    count: int


class UpcomingSessionItem(BaseModel):
    date: date
    time: str
    client_name: str
    therapist_name: str
    status: str


class FollowUpQueueItem(BaseModel):
    client_name: str
    due_date: date
    notes: str | None
    status: str


class TherapistUtilizationItem(BaseModel):
    therapist_name: str
    location_name: str
    sessions_this_week: int
    target: int = 8


class DashboardResponse(BaseModel):
    leads: LeadStat
    clients: ClientStat
    pending_follow_ups: int
    sessions_today: int
    session_metrics: SessionMetrics
    revenue_metrics: RevenueMetrics
    revenue_trend: list[RevenueTrendPoint]
    payment_status: list[PaymentStatusCount]
    lead_funnel: list[FunnelStage]
    upcoming_sessions: list[UpcomingSessionItem]
    follow_up_queue: list[FollowUpQueueItem]
    therapist_utilization: list[TherapistUtilizationItem]