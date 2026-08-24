from datetime import datetime, timedelta, timezone, date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.follow_up import FollowUp
from app.models.session import Session as SessionModel
from app.models.enums import SessionStatus
from app.services.notification_service import create_notification
from app.services.audit_context import install_context, reset_context, system_context
from app.services.audit_service import purge_expired, record_read
from app.services.retention_service import run_retention_sweep
from app.config import settings
from app.models.notification import NotificationCategory, NotificationBadge
from app.models.client import Client
from zoneinfo import ZoneInfo

APPOINTMENT_LOOKAHEAD_HOURS = 2
PAYMENT_DUE_SOON_DAYS = 3
EASTERN = ZoneInfo("America/New_York")

async def _scan_follow_ups(db) -> list:
    read_ids = []
    today = datetime.now(EASTERN).date()
    tomorrow = today + timedelta(days=1)

    result = await db.execute(
        select(FollowUp).where(FollowUp.completed_at.is_(None), FollowUp.due_date.in_([today, tomorrow]))
    )
    for fu in result.scalars().all():
        read_ids.append(fu.id)
        client = await db.get(Client, fu.client_id)
        therapist_id = getattr(client, "therapist_id", None)
        label = "today" if fu.due_date == today else "tomorrow"
        await create_notification(
            db, NotificationCategory.FOLLOW_UP_REMINDER, NotificationBadge.REMINDER,
            title=f"Follow-up due {label}",
            body=f"Follow-up for {client.name if client else 'a client'} is due {label}.",
            therapist_id=therapist_id,
            related_entity_type="follow_up", related_entity_id=fu.id,
            commit=False,
        )

    result = await db.execute(
        select(FollowUp).where(FollowUp.completed_at.is_(None), FollowUp.due_date < today)
    )
    for fu in result.scalars().all():
        read_ids.append(fu.id)
        client = await db.get(Client, fu.client_id)
        therapist_id = getattr(client, "therapist_id", None)
        await create_notification(
            db, NotificationCategory.FOLLOW_UP_REMINDER, NotificationBadge.OVERDUE,
            title="Follow-up overdue",
            body=f"{client.name if client else 'A client'}'s follow-up is overdue.",
            therapist_id=therapist_id,
            related_entity_type="follow_up", related_entity_id=fu.id,
            commit=False,
        )
    await db.commit()
    return read_ids

async def _scan_sessions(db) -> list:
    read_ids = []
    now = datetime.now(EASTERN)
    window_end = now + timedelta(hours=APPOINTMENT_LOOKAHEAD_HOURS)

    result = await db.execute(
        select(SessionModel).where(SessionModel.status == SessionStatus.SCHEDULED)
    )
    for s in result.scalars().all():
        read_ids.append(s.id)
        session_dt = datetime.combine(s.date, s.time, tzinfo=EASTERN)
        if now <= session_dt <= window_end:
            await create_notification(
                db, NotificationCategory.APPOINTMENT_REMINDER, NotificationBadge.REMINDER,
                title="Upcoming appointment",
                body=f"Session scheduled at {s.time.strftime('%I:%M %p')} today.",
                therapist_id=s.therapist_id,
                related_entity_type="session", related_entity_id=s.id,
                commit=False,
            )
    await db.commit()
    return read_ids

async def run_notification_scan():
    """The 15-minute reminder sweep.

    Audit item 7.3: this reads every open follow-up, the client behind each one
    and every scheduled session, on a timer, entirely outside the request and
    auth layers. Before this it was the most regular PHI access in the system
    and the only one with no identity attached at all — so it declares one, and
    every audit row it causes (the notifications it writes as well as the reads
    recorded below) is attributed to "system:scheduler" rather than to nobody.

    The reads are recorded AFTER both scans finish rather than interleaved,
    because record_read commits and doing that mid-scan would split the sweep
    into partial transactions it was not written to tolerate.
    """
    token = install_context(system_context("system:scheduler"))
    try:
        async with AsyncSessionLocal() as db:
            follow_up_ids = await _scan_follow_ups(db)
            session_ids = await _scan_sessions(db)
            if follow_up_ids:
                await record_read(db, "follow_up", entity_ids=follow_up_ids,
                                  criteria={"job": "notification_scan"})
            if session_ids:
                await record_read(db, "session", entity_ids=session_ids,
                                  criteria={"job": "notification_scan"})
    finally:
        reset_context(token)

async def run_audit_retention() -> int:
    """Enforce the audit retention ceiling.

    Runs daily and does nothing at all for six years, which is the point: the
    MECHANISM has to exist before it is needed. This codebase already carries
    three tables with a TODO(retention) comment and nowhere to hang a policy —
    import_rows.raw_payload, idempotency_keys.response_body, otp_codes — and
    that is precisely how a retention requirement quietly goes unmet.

    Attributed to "system:retention" so the one path that deletes audit rows is
    itself attributable.
    """
    if not settings.AUDIT_PURGE_ENABLED:
        return 0
    token = install_context(system_context("system:retention"))
    try:
        async with AsyncSessionLocal() as db:
            return await purge_expired(db, settings.AUDIT_RETENTION_DAYS)
    finally:
        reset_context(token)


async def run_data_retention() -> dict:
    """Enforce retention on the other PHI-bearing tables (item 5.7).

    Separate from the audit purge because they answer to different windows and
    different rules — audit rows are kept for six years and then deleted, while
    import rows are redacted in place after thirty days so the import stays
    explainable. Sharing the same system identity so both are attributable.
    """
    token = install_context(system_context("system:retention"))
    try:
        async with AsyncSessionLocal() as db:
            return await run_retention_sweep(db)
    finally:
        reset_context(token)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_notification_scan, "interval", minutes=15, id="notification_scan")
    # Daily rather than hourly: it is a ceiling, not a deadline, and a delete
    # over an indexed created_at range is cheap enough that frequency buys
    # nothing.
    scheduler.add_job(run_audit_retention, "interval", hours=24, id="audit_retention")
    scheduler.add_job(run_data_retention, "interval", hours=24, id="data_retention")
    scheduler.start()
    return scheduler