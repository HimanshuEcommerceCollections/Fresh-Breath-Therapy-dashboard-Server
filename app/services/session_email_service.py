"""Emails to the person a session is for: a confirmation when it is booked,
and a reminder 24 hours before it starts.

Both are best-effort. A failed send is logged and never fails the booking —
the session is real whether or not the email arrived. The reminder is retried
on every scan until it goes out or the session starts.

Session times are clinic-local (Eastern), the same assumption the in-app
appointment reminders in scheduler_service.py make.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.enums import SessionStatus
from app.models.session import Session
from app.services.email_service import (
    send_session_reminder_email, send_session_scheduled_email,
)

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
REMINDER_BEFORE = timedelta(hours=24)


def session_starts_at(session: Session) -> datetime:
    return datetime.combine(session.date, session.time, tzinfo=EASTERN)


def _recipient(session: Session) -> tuple[str, str] | None:
    """(email, name) of the client or lead the session is for."""
    subject = session.client or session.lead
    if subject is None or not subject.email:
        return None
    return subject.email, subject.name


def reminder_already_covered(session: Session, now: datetime | None = None) -> bool:
    """A session booked less than 24 hours out never gets a separate reminder:
    the booking confirmation, sent moments ago, already is one."""
    now = now or datetime.now(timezone.utc)
    return session_starts_at(session) - REMINDER_BEFORE <= now


async def send_scheduled_confirmation(session: Session) -> None:
    """Confirmation for a just-booked session. Needs client, lead and
    therapist loaded. Never raises."""
    if not settings.EMAIL_SERVICE or session.status != SessionStatus.SCHEDULED:
        return
    recipient = _recipient(session)
    if recipient is None:
        return
    email, name = recipient
    try:
        await run_in_threadpool(
            send_session_scheduled_email,
            email, name, session.date, session.time, session.therapist.name,
        )
    except Exception:
        logger.exception("Session confirmation email failed for session %s", session.id)


async def send_due_reminders(db: AsyncSession) -> list:
    """Send every reminder whose moment has come: the session starts within
    the next 24 hours and no reminder has gone out yet.

    Runs inside the 15-minute notification scan, so a reminder lands within
    one scan interval after the 24-hour mark. Each send is committed on its
    own, so a crash part-way never re-sends the ones already delivered.
    Returns the ids of the sessions read, for the audit trail.
    """
    if not settings.EMAIL_SERVICE:
        return []

    now = datetime.now(timezone.utc)
    today = now.astimezone(EASTERN).date()
    # Date-bounded so the scan never walks the whole calendar; the exact
    # 24-hour window is checked per row below.
    result = await db.execute(
        select(Session)
        .options(
            selectinload(Session.client),
            selectinload(Session.lead),
            selectinload(Session.therapist),
        )
        .where(
            Session.status == SessionStatus.SCHEDULED,
            Session.reminder_sent_at.is_(None),
            Session.date >= today,
            Session.date <= today + timedelta(days=2),
        )
    )

    read_ids = []
    for session in result.scalars().all():
        starts_at = session_starts_at(session)
        if not (starts_at - REMINDER_BEFORE <= now < starts_at):
            continue
        read_ids.append(session.id)
        recipient = _recipient(session)
        if recipient is None:
            continue
        email, name = recipient
        try:
            await run_in_threadpool(
                send_session_reminder_email,
                email, name, session.date, session.time, session.therapist.name,
            )
        except Exception:
            # Left unmarked, so the next scan tries again.
            logger.exception("Session reminder email failed for session %s", session.id)
            continue
        session.reminder_sent_at = now
        await db.commit()
    return read_ids
