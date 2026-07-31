import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from app.models.otp_code import OtpCode
from app.services.security import hash_password, verify_password
from app.services.email_service import send_otp_email

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 5
RESEND_COOLDOWN_SECONDS = 5  # TODO: revert to 5-minute cooldown (RESEND_COOLDOWN_MINUTES = 5) after testing


def _generate_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


async def request_otp(db: AsyncSession, user_id: uuid.UUID, email: str, purpose: str) -> datetime:
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(OtpCode)
        .where(OtpCode.user_id == user_id, OtpCode.purpose == purpose, OtpCode.consumed == False)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()

    if existing and existing.last_sent_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS) > now:
        wait_seconds = int((existing.last_sent_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS) - now).total_seconds())
        raise HTTPException(
            status_code=429,
            detail=f"An OTP was already sent. Please wait {wait_seconds} seconds before requesting another.",
        )

    code = _generate_code()
    expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)

    if existing:
        existing.code_hash = hash_password(code)
        existing.expires_at = expires_at
        existing.last_sent_at = now
    else:
        db.add(OtpCode(
            id=uuid.uuid4(), user_id=user_id, code_hash=hash_password(code),
            purpose=purpose, expires_at=expires_at, last_sent_at=now, consumed=False,
        ))

    await db.commit()

    try:
        # Sent off-loop via run_in_threadpool since smtplib is blocking, and
        # this project's FastAPI process serves multiple concurrent requests
        # on one event loop (see the note in email_service.py) — awaited
        # synchronously so the request only completes once the email is
        # actually sent, with no background retry to fall back on.
        await run_in_threadpool(send_otp_email, email, code)
    except Exception:
        logger.exception(f"Failed to send OTP email to {email}")
        raise HTTPException(
            status_code=502,
            detail="Failed to send verification email. Please try again in a moment.",
        )

    return expires_at


async def verify_otp(db: AsyncSession, user_id: uuid.UUID, purpose: str, code: str) -> bool:
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(OtpCode)
        .where(OtpCode.user_id == user_id, OtpCode.purpose == purpose, OtpCode.consumed == False)
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    otp = result.scalar_one_or_none()

    if otp is None:
        raise HTTPException(status_code=400, detail="No OTP request found. Please request a new code.")
    if otp.expires_at < now:
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new code.")
    if not verify_password(code, otp.code_hash):
        raise HTTPException(status_code=400, detail="Incorrect code.")

    otp.consumed = True
    await db.commit()
    return True