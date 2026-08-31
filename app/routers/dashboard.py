from datetime import date, timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, literal

from app.database import get_db
from app.services.audit_service import record_read
from app.models.lead import Lead
from app.models.client import Client
from app.models.therapist import Therapist
from app.models.location import Location
from app.models.session import Session as SessionModel
from app.models.payment import Payment
from app.models.enrollment import Enrollment
from app.models.follow_up import FollowUp
from app.models.enums import LeadStatus, ClientStatus, SessionStatus, EnrollmentStatus, PaymentStatus
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

    # Every single-value aggregate on this page, in ONE round trip.
    #
    # These were eleven separate `await db.execute(...)` calls, each a full
    # round trip issued strictly one after another. Against a database a few
    # hundred milliseconds away that alone was most of the ten seconds the
    # dashboard took to load — and it was pure latency, not work: each of
    # these is an indexed COUNT or SUM that Postgres answers in microseconds.
    #
    # As scalar subqueries in a single SELECT the database does exactly the
    # same work, and we wait for the network once instead of eleven times.
    # The multi-row queries below genuinely return sets and stay as they are.
    def _count(model, *where):
        q = select(func.count(model.id))
        return (q.where(*where) if where else q).scalar_subquery()

    def _sum(column, *where):
        q = select(func.coalesce(func.sum(column), 0))
        return (q.where(*where) if where else q).scalar_subquery()

    totals = (await db.execute(select(
        _count(Lead).label("total_leads"),
        _count(Lead, Lead.created_at >= month_start).label("new_leads"),
        _count(Client, Client.status != ClientStatus.COMPLETED_PROGRAM).label("active_clients"),
        _count(Client, Client.created_at >= thirty_days_ago).label("new_clients"),
        _count(FollowUp, FollowUp.due_date >= today,
               FollowUp.completed_at.is_(None)).label("pending_follow_ups"),
        _count(SessionModel, SessionModel.date == today).label("sessions_today"),
        _count(SessionModel, SessionModel.status == SessionStatus.SCHEDULED,
               SessionModel.date >= today).label("upcoming"),
        _sum(Enrollment.package_price_snapshot).label("revenue_totals"),
        _sum(Payment.amount_paid).label("collected"),
        _sum(Enrollment.amount_due,
             Enrollment.status == EnrollmentStatus.ACTIVE).label("pending_payments"),
        _sum(Payment.amount_paid, Payment.date >= month_start).label("monthly_revenue"),
    ))).one()

    total_leads = totals.total_leads
    new_leads = totals.new_leads
    active_clients = totals.active_clients
    new_clients = totals.new_clients
    pending_follow_ups = totals.pending_follow_ups
    sessions_today = totals.sessions_today

    # Session metrics — one grouped query for status counts, one for "today"/"upcoming" special cases
    status_count_rows = (await db.execute(
        select(SessionModel.status, func.count(SessionModel.id)).group_by(SessionModel.status)
    )).all()
    status_counts = {row[0]: row[1] for row in status_count_rows}
    total_sessions = sum(status_counts.values())
    upcoming = totals.upcoming

    session_metrics = SessionMetrics(
        total=total_sessions,
        completed=status_counts.get(SessionStatus.COMPLETED, 0),
        upcoming=upcoming,
        missed=status_counts.get(SessionStatus.NO_SHOW, 0),
        cancelled=status_counts.get(SessionStatus.CANCELLED, 0),
        today=sessions_today,
    )

    # Revenue — total_revenue is the total contracted value of every
    # enrollment ever started (active + completed); collected is actual cash
    # taken in via the payments ledger. Neither figure lives on a single row
    # anymore now that "due" isn't stored per-payment — see Enrollment.
    total_revenue = Decimal(str(totals.revenue_totals))
    collected = Decimal(str(totals.collected))
    pending_payments = Decimal(str(totals.pending_payments))

    # Quantize to cents — an unrounded Decimal division serialises as a
    # 24-decimal-place string in the JSON response.
    avg_per_client = (
        (total_revenue / active_clients).quantize(Decimal("0.01"))
        if active_clients else Decimal("0.00")
    )

    monthly_revenue = Decimal(str(totals.monthly_revenue))

    revenue_metrics = RevenueMetrics(
        total_revenue=total_revenue,
        monthly_revenue=monthly_revenue,
        pending_payments=pending_payments,
        collected=collected,
        avg_per_client=avg_per_client,
    )

    # Revenue trend — "collected" is cash actually taken in that month
    # (sum of ledger rows dated in it). "pending" is the amount still owed,
    # as of today, from enrollments that were STARTED in that month — a
    # ledger has no per-historical-month "amount still due" figure the way
    # a due/paid snapshot row used to, so this is the closest still-useful
    # analogue: "how much of the business opened that month is unpaid now".
    collected_month_expr = func.to_char(Payment.date, "YYYY-MM").label("month")
    collected_rows = (await db.execute(
        select(collected_month_expr, func.coalesce(func.sum(Payment.amount_paid), 0))
        .where(Payment.date >= six_months_ago)
        .group_by(collected_month_expr)
    )).all()
    collected_by_month = {row[0]: Decimal(str(row[1])) for row in collected_rows}

    pending_month_expr = func.to_char(Enrollment.started_at, "YYYY-MM").label("month")
    pending_rows = (await db.execute(
        select(pending_month_expr, func.coalesce(func.sum(Enrollment.amount_due), 0))
        .where(Enrollment.started_at >= six_months_ago, Enrollment.status == EnrollmentStatus.ACTIVE)
        .group_by(pending_month_expr)
    )).all()
    pending_by_month = {row[0]: Decimal(str(row[1])) for row in pending_rows}

    all_months = sorted(set(collected_by_month) | set(pending_by_month))
    revenue_trend = [
        RevenueTrendPoint(
            month=month,
            collected=collected_by_month.get(month, Decimal("0")),
            pending=pending_by_month.get(month, Decimal("0")),
        )
        for month in all_months
    ]

    # Invoice payment-status distribution — the same four states the Payments
    # page shows (paid / partially paid / pending / overdue), derived in SQL so
    # this donut can't disagree with that table. Mirrors
    # Enrollment.payment_status; keep the two in step if either changes.
    status_expr = case(
        (Enrollment.is_overdue.is_(True), literal(PaymentStatus.OVERDUE.value)),
        (Enrollment.total_paid <= 0, literal(PaymentStatus.PENDING.value)),
        (Enrollment.total_paid >= Enrollment.package_price_snapshot,
         literal(PaymentStatus.PAID.value)),
        else_=literal(PaymentStatus.PARTIALLY_PAID.value),
    )
    status_rows = (await db.execute(
        select(status_expr.label("s"), func.count(Enrollment.id)).group_by(status_expr)
    )).all()
    counts_by_status = {row[0]: row[1] for row in status_rows}
    payment_status = [
        PaymentStatusCount(status=s.value, count=counts_by_status.get(s.value, 0))
        for s in PaymentStatus
        if counts_by_status.get(s.value, 0) > 0
    ]

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

    await record_read(db, "dashboard", criteria={"range": "default"})
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