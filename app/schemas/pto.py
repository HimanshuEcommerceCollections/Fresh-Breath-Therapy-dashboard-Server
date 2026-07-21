import uuid
from decimal import Decimal
from pydantic import BaseModel


class PtoStats(BaseModel):
    total_therapists: int
    total_sessions: int
    pto_accrued: Decimal
    pto_used: Decimal
    pto_balance: Decimal


class LocationPtoPoint(BaseModel):
    location_id: uuid.UUID
    location_name: str
    therapist_count: int
    session_count: int
    pto_hours: Decimal


class LeaderboardItem(BaseModel):
    rank: int
    therapist_id: uuid.UUID
    therapist_name: str
    credential: str | None
    location_name: str
    ytd_sessions: int
    pto_accrued: Decimal
    pto_used: Decimal
    balance: Decimal
    avg_per_week: Decimal


class PtoDashboardResponse(BaseModel):
    stats: PtoStats
    by_location: list[LocationPtoPoint]
    leaderboard: list[LeaderboardItem]