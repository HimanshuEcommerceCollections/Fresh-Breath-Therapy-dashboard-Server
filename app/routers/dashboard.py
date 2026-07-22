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
from app.models.follow_up import FollowUp
from app.models.enums import LeadStatus, ClientStatus, SessionStatus, PaymentStatus
from app.schemas.dashboard import (
    DashboardResponse, LeadStat, ClientStat, SessionMetrics, RevenueMetrics,
    RevenueTrendPoint, PaymentStatusCount, FunnelStage, UpcomingSessionItem,
    FollowUpQueueItem, TherapistUtilizationItem,
)
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    today = date.today()
    month_start = today.replace(day=1)
    thirty_days_ago = today - timedelta(days=30)
    six_months_ago = today - timedelta(days=182)
    week_start = today - timedelta(days=today.weekday())

    # Leads
    total_leads = (await db.execute(select(func.count(Lead.id)))).scalar_one()
    new_leads = (await db.execute(
        select(func.count(Lead.id)).where(Lead.created_at >= month_start)
    )).scalar_one()

    # Clients
    active_clients = (await db.execute(
        select(func.count(Client.id)).where(Client.status != ClientStatus.COMPLETED_PROGRAM)
    )).scalar_one()
    new_clients = (await db.execute(
        select(func.count(Client.id)).where(Client.created_at >= thirty_days_ago)
    )).scalar_one()

    # Follow-ups
    pending_follow_ups = (await db.execute(
        select(func.count(FollowUp.id)).where(
            FollowUp.due_date >= today, FollowUp.completed_at.is_(None)
        )
    )).scalar_one()

    # Sessions today
    sessions_today = (await db.execute(
        select(func.count(SessionModel.id)).where(SessionModel.date == today)
    )).scalar_one()

    # Session metrics — one grouped query for status counts, one for "today"/"upcoming" special cases
    status_count_rows = (await db.execute(
        select(SessionModel.status, func.count(SessionModel.id)).group_by(SessionModel.status)
    )).all()
    status_counts = {row[0]: row[1] for row in status_count_rows}
    total_sessions = sum(status_counts.values())
    upcoming = (await db.execute(
        select(func.count(SessionModel.id)).where(
            SessionModel.status == SessionStatus.SCHEDULED, SessionModel.date >= today
        )
    )).scalar_one()

    session_metrics = SessionMetrics(
        total=total_sessions,
        completed=status_counts.get(SessionStatus.COMPLETED, 0),
        upcoming=upcoming,
        missed=status_counts.get(SessionStatus.NO_SHOW, 0),
        cancelled=status_counts.get(SessionStatus.CANCELLED, 0),
        today=sessions_today,
    )

    # Revenue — org-wide sums, one query
    revenue_totals = (await db.execute(
        select(func.coalesce(func.sum(Payment.due), 0), func.coalesce(func.sum(Payment.paid), 0))
    )).one()
    total_due, total_paid = Decimal(str(revenue_totals[0])), Decimal(str(revenue_totals[1]))
    pending_payments = total_due - total_paid
    total_revenue = total_due
    collected = total_paid
    avg_per_client = (total_revenue / active_clients) if active_clients else Decimal("0")

    monthly_revenue = (await db.execute(
        select(func.coalesce(func.sum(Payment.paid), 0)).where(Payment.date >= month_start)
    )).scalar_one()
    monthly_revenue = Decimal(str(monthly_revenue))

    revenue_metrics = RevenueMetrics(
        total_revenue=total_revenue,
        monthly_revenue=monthly_revenue,
        pending_payments=pending_payments,
        collected=collected,
        avg_per_client=avg_per_client,
    )

    # Revenue trend — one grouped query
    trend_rows = (await db.execute(
        select(
            func.to_char(Payment.date, "YYYY-MM"),
            func.coalesce(func.sum(Payment.paid), 0),
            func.coalesce(func.sum(Payment.due - Payment.paid), 0),
        )
        .where(Payment.date >= six_months_ago)
        .group_by(func.to_char(Payment.date, "YYYY-MM"))
        .order_by(func.to_char(Payment.date, "YYYY-MM"))
    )).all()
    revenue_trend = [
        RevenueTrendPoint(month=row[0], collected=row[1], pending=row[2]) for row in trend_rows
    ]

    # Payment status distribution — one grouped query
    payment_status_rows = (await db.execute(
        select(Payment.status, func.count(Payment.id)).group_by(Payment.status)
    )).all()
    payment_status = [PaymentStatusCount(status=row[0].value, count=row[1]) for row in payment_status_rows]

    # Lead funnel — one grouped query, filled to all 8 statuses
    funnel_rows = (await db.execute(
        select(Lead.status, func.count(Lead.id)).group_by(Lead.status)
    )).all()
    funnel_counts = {row[0]: row[1] for row in funnel_rows}
    lead_funnel = [
        FunnelStage(status=s.value, count=funnel_counts.get(s, 0)) for s in LeadStatus
    ]

    # Upcoming sessions — one JOIN query, no per-row lookups
    upcoming_rows = (await db.execute(
        select(SessionModel, Client.name, Therapist.name)
        .join(Client, SessionModel.client_id == Client.id)
        .join(Therapist, SessionModel.therapist_id == Therapist.id)
        .where(SessionModel.status == SessionStatus.SCHEDULED, SessionModel.date >= today)
        .order_by(SessionModel.date, SessionModel.time)
        .limit(5)
    )).all()
    upcoming_sessions = [
        UpcomingSessionItem(
            date=s.date, time=str(s.time), client_name=client_name,
            therapist_name=therapist_name, status=s.status.value,
        )
        for s, client_name, therapist_name in upcoming_rows
    ]

    # Follow-up queue — one JOIN query, no per-row lookups
    overdue_rows = (await db.execute(
        select(FollowUp, Client.name)
        .join(Client, FollowUp.client_id == Client.id)
        .where(FollowUp.due_date < today, FollowUp.completed_at.is_(None))
        .order_by(FollowUp.due_date)
        .limit(5)
    )).all()
    follow_up_queue = [
        FollowUpQueueItem(client_name=client_name, due_date=f.due_date, notes=f.notes, status="overdue")
        for f, client_name in overdue_rows
    ]

    # Therapist utilization — one JOIN+GROUP BY query for the week's session counts,
    # left-joined so therapists with 0 sessions this week still appear
    utilization_rows = (await db.execute(
        select(Therapist.name, Location.name, func.count(SessionModel.id))
        .join(Location, Therapist.location_id == Location.id)
        .outerjoin(
            SessionModel,
            (SessionModel.therapist_id == Therapist.id) & (SessionModel.date >= week_start),
        )
        .group_by(Therapist.id, Therapist.name, Location.name)
        .order_by(func.count(SessionModel.id).desc())
        .limit(5)
    )).all()
    therapist_utilization = [
        TherapistUtilizationItem(therapist_name=row[0], location_name=row[1], sessions_this_week=row[2])
        for row in utilization_rows
    ]

    return DashboardResponse(
        leads=LeadStat(total=total_leads, new_this_month=new_leads),
        clients=ClientStat(active=active_clients, new_last_30_days=new_clients),
        pending_follow_ups=pending_follow_ups,
        sessions_today=sessions_today,
        session_metrics=session_metrics,
        revenue_metrics=revenue_metrics,
        revenue_trend=revenue_trend,
        payment_status=payment_status,
        lead_funnel=lead_funnel,
        upcoming_sessions=upcoming_sessions,
        follow_up_queue=follow_up_queue,
        therapist_utilization=therapist_utilization,
    )