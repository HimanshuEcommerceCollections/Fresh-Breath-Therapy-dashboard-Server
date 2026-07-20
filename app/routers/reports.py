import uuid
from datetime import date, timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models.lead import Lead
from app.models.client import Client
from app.models.therapist import Therapist
from app.models.location import Location
from app.models.session import Session as SessionModel
from app.models.payment import Payment
from app.models.enums import LeadStatus, SessionStatus
from app.schemas.report import (
    SalesPoint, ClientStatusPoint, TeamPerformancePoint,
    ConversionReport, ConversionStage, UtilizationPoint,
    RevenuePoint, RetentionPoint,
)
from app.models.user import User
from app.dependencies.auth import get_current_user

router = APIRouter(prefix="/api/reports", tags=["reports"])

RANGE_DAYS = {
    "last_30_days": 30,
    "last_3_months": 90,
    "last_6_months": 182,
    "last_12_months": 365,
}


def _range_to_start_date(range_: str) -> date | None:
    days = RANGE_DAYS.get(range_)
    return date.today() - timedelta(days=days) if days else None


@router.get("/sales", response_model=list[SalesPoint])
async def sales_report(
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_date = _range_to_start_date(range)
    query = (
        select(func.to_char(Payment.date, "YYYY-MM").label("month"),
               func.coalesce(func.sum(Payment.paid), 0))
        .join(Client, Payment.client_id == Client.id)
    )
    if location_id:
        query = query.where(Client.location_id == location_id)
    if start_date:
        query = query.where(Payment.date >= start_date)
    query = query.group_by("month").order_by("month")

    result = await db.execute(query)
    return [SalesPoint(month=row[0], total=row[1]) for row in result.all()]


async def _lead_status_counts(db, location_id, start_date):
    query = select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
    if location_id:
        query = query.where(Lead.location_id == location_id)
    if start_date:
        query = query.where(Lead.created_at >= start_date)
    result = await db.execute(query)
    counts = {row[0]: row[1] for row in result.all()}
    return {s: counts.get(s, 0) for s in LeadStatus}


@router.get("/clients", response_model=list[ClientStatusPoint])
async def clients_by_status_report(
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_date = _range_to_start_date(range)
    counts = await _lead_status_counts(db, location_id, start_date)
    return [ClientStatusPoint(status=s.value, count=c) for s, c in counts.items()]


@router.get("/team", response_model=list[TeamPerformancePoint])
async def team_performance_report(
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_date = _range_to_start_date(range)
    query = (
        select(Therapist.id, Therapist.name, func.count(SessionModel.id))
        .outerjoin(SessionModel, SessionModel.therapist_id == Therapist.id)
        .group_by(Therapist.id, Therapist.name)
        .order_by(func.count(SessionModel.id).desc())
    )
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    if start_date:
        query = query.where(
            (SessionModel.date >= start_date) | (SessionModel.date.is_(None))
        )

    result = await db.execute(query)
    return [
        TeamPerformancePoint(therapist_id=row[0], therapist_name=row[1], sessions=row[2])
        for row in result.all()
    ]


@router.get("/conversion", response_model=ConversionReport)
async def conversion_report(
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_date = _range_to_start_date(range)
    counts = await _lead_status_counts(db, location_id, start_date)
    total = sum(counts.values())

    converted_query = select(func.count(Lead.id)).where(Lead.converted_client_id.is_not(None))
    if location_id:
        converted_query = converted_query.where(Lead.location_id == location_id)
    if start_date:
        converted_query = converted_query.where(Lead.created_at >= start_date)
    converted = (await db.execute(converted_query)).scalar_one()

    overall_rate = round((converted / total * 100), 1) if total else 0.0

    stages = [
        ConversionStage(
            status=s.value,
            count=c,
            percent=round((c / total * 100), 1) if total else 0.0,
        )
        for s, c in counts.items()
    ]

    return ConversionReport(overall_rate=overall_rate, total_leads=total, stages=stages)


@router.get("/utilization", response_model=list[UtilizationPoint])
async def utilization_report(
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Therapist.id, Therapist.name, Therapist.created_at)
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    therapists = (await db.execute(query)).all()

    points = []
    for therapist_id, name, created_at in therapists:
        completed_query = select(func.count(SessionModel.id)).where(
            SessionModel.therapist_id == therapist_id,
            SessionModel.status == SessionStatus.COMPLETED,
        )
        completed = (await db.execute(completed_query)).scalar_one()

        weeks_active = max((date.today() - created_at.date()).days / 7, 1)
        utilization = round(completed / weeks_active, 1)

        points.append(UtilizationPoint(therapist_id=therapist_id, therapist_name=name, utilization=utilization))

    return sorted(points, key=lambda p: p.utilization, reverse=True)


@router.get("/revenue", response_model=list[RevenuePoint])
async def revenue_by_therapist_report(
    range: str = "last_6_months",
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_date = _range_to_start_date(range)
    query = (
        select(Therapist.id, Therapist.name, func.coalesce(func.sum(Payment.paid), 0))
        .join(Client, Client.therapist_id == Therapist.id)
        .join(Payment, Payment.client_id == Client.id)
        .group_by(Therapist.id, Therapist.name)
        .order_by(func.sum(Payment.paid).desc())
    )
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    if start_date:
        query = query.where(Payment.date >= start_date)

    result = await db.execute(query)
    return [
        RevenuePoint(therapist_id=row[0], therapist_name=row[1], revenue=row[2])
        for row in result.all()
    ]


@router.get("/retention", response_model=list[RetentionPoint])
async def retention_by_location_report(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    locations = (await db.execute(select(Location.id, Location.name))).all()

    points = []
    for location_id, location_name in locations:
        clients = (
            await db.execute(
                select(Client.id, Client.created_at).where(Client.location_id == location_id)
            )
        ).all()

        if not clients:
            points.append(RetentionPoint(location_id=location_id, location_name=location_name, retention_months=0.0))
            continue

        total_months = 0.0
        for client_id, created_at in clients:
            last_session = (
                await db.execute(
                    select(func.max(SessionModel.date)).where(SessionModel.client_id == client_id)
                )
            ).scalar_one()
            end_date = last_session or date.today()
            months = max((end_date - created_at.date()).days / 30, 0)
            total_months += months

        points.append(RetentionPoint(
            location_id=location_id,
            location_name=location_name,
            retention_months=round(total_months / len(clients), 1),
        ))

    return points