from datetime import datetime, timedelta, timezone, date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.follow_up import FollowUp
from app.models.session import Session as SessionModel
from app.models.payment import Payment
from app.models.enums import SessionStatus, PaymentStatus
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge
from app.models.client import Client
from zoneinfo import ZoneInfo

APPOINTMENT_LOOKAHEAD_HOURS = 2
PAYMENT_DUE_SOON_DAYS = 3
EASTERN = ZoneInfo("America/New_York")

async def _scan_follow_ups(db):
    today = datetime.now(EASTERN).date()
    tomorrow = today + timedelta(days=1)

    result = await db.execute(
        select(FollowUp).where(FollowUp.completed_at.is_(None), FollowUp.due_date.in_([today, tomorrow]))
    )
    for fu in result.scalars().all():
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

async def _scan_sessions(db):
    now = datetime.now(EASTERN)
    window_end = now + timedelta(hours=APPOINTMENT_LOOKAHEAD_HOURS)

    result = await db.execute(
        select(SessionModel).where(SessionModel.status == SessionStatus.SCHEDULED)
    )
    for s in result.scalars().all():
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

async def run_notification_scan():
    async with AsyncSessionLocal() as db:
        await _scan_follow_ups(db)
        await _scan_sessions(db)

def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(run_notification_scan, "interval", minutes=15, id="notification_scan")
    scheduler.start()
    return scheduler