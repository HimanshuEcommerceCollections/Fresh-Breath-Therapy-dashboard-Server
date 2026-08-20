"""OTP issue and verify, bound to the credential check that started it.

THE RULE THIS MODULE EXISTS TO ENFORCE: an OTP row is addressable only by its
ticket, never by the user's email.

Why it matters. Login is two requests — /login checks the password, then
/verify-login-otp checks the code and mints the session. The second request
used to find the OTP row by the email in its own body, which means it decided
whose session to create from an attacker-supplied, publicly-known string. That
was a complete authentication bypass: POST an admin's address with any code and
the endpoint handed back that admin's cookie, because the password was verified
in a *different* endpoint the attacker never had to call.

A ticket fixes it structurally rather than with another conditional. /login
mints one only after verify_password succeeds (and the Google callback only
after Google asserts the identity), returns it as an httpOnly cookie, and
stores nothing but its SHA-256 digest here. From then on the ticket is the
only handle on that login attempt: no ticket, no session, whatever the request
body claims. The email in the body survives as a cross-check, not a lookup key.
"""
import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool

from app.models.otp_code import OtpCode
from app.models.user import User
from app.services.security import hash_password, verify_password
from app.services.email_service import send_otp_email

logger = logging.getLogger(__name__)

OTP_TTL_MINUTES = 5
RESEND_COOLDOWN_SECONDS = 5 * 60
# Wrong codes tolerated per issued code. Five is enough to absorb fat-fingering
# a 6-digit code and far too few to search a 1,000,000-wide space. Hitting it
# burns the row: the user goes back through the password step, which is the
# thing an attacker cannot do.
MAX_VERIFY_ATTEMPTS = 5

# Message deliberately identical for "no ticket", "unknown ticket" and
# "already spent" — the difference between them is information about another
# account's state, and the remedy is the same in all three cases.
_NO_SESSION_DETAIL = "This login session is no longer valid. Please sign in again."


def _generate_code() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def hash_ticket(ticket: str) -> str:
    """SHA-256, deliberately not bcrypt.

    bcrypt is for low-entropy secrets a human chose; its per-hash salt also
    makes the digest impossible to look a row up BY, which is exactly what a
    ticket has to support. A 256-bit random value needs no work factor — there
    is nothing to guess — so a plain fast digest is both correct and indexable.
    """
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _new_ticket() -> tuple[str, str]:
    """(ticket handed to the client, digest stored here). 32 bytes = 256 bits."""
    ticket = secrets.token_urlsafe(32)
    return ticket, hash_ticket(ticket)


async def resolve_ticket(db: AsyncSession, ticket: str | None, purpose: str) -> OtpCode:
    """The live OTP row this ticket owns, with its user and role loaded.

    One round trip, not three: `user` is a many-to-one and `role` behind it is
    another, so both ride along as LEFT JOINs rather than follow-up SELECTs —
    the caller needs the user to mint a session and the role to serialise it.

    Raises 401 rather than returning None. There is no caller for whom a
    missing ticket is a recoverable state.
    """
    if not ticket:
        raise HTTPException(status_code=401, detail=_NO_SESSION_DETAIL)

    result = await db.execute(
        select(OtpCode)
        .options(joinedload(OtpCode.user).joinedload(User.role))
        .where(
            OtpCode.ticket_hash == hash_ticket(ticket),
            OtpCode.purpose == purpose,
            OtpCode.consumed == False,  # noqa: E712 — SQL comparison, not Python
        )
    )
    otp = result.unique().scalar_one_or_none()
    if otp is None:
        raise HTTPException(status_code=401, detail=_NO_SESSION_DETAIL)
    return otp


async def request_otp(
    db: AsyncSession, user_id: uuid.UUID, email: str, purpose: str
) -> tuple[datetime, str]:
    """Issue (or re-issue) a code, and return (expires_at, ticket).

    The ticket MUST be handed to the client as an httpOnly cookie and never in
    a response body, a URL or a log — see auth_cookie.set_login_ticket_cookie.

    Callers must have authenticated the user first. This function is the second
    factor; it is not itself a credential check.
    """
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
    ticket, ticket_hash = _new_ticket()
    expires_at = now + timedelta(minutes=OTP_TTL_MINUTES)

    if existing:
        existing.code_hash = hash_password(code)
        existing.expires_at = expires_at
        existing.last_sent_at = now
        # Rotate, don't append: issuing a new code kills the ticket that
        # fetched the old one, so a resend can't leave two live handles on the
        # same login attempt.
        existing.ticket_hash = ticket_hash
        existing.attempts = 0  # new code, new budget
    else:
        db.add(OtpCode(
            id=uuid.uuid4(), user_id=user_id, code_hash=hash_password(code),
            purpose=purpose, ticket_hash=ticket_hash, attempts=0,
            expires_at=expires_at, last_sent_at=now, consumed=False,
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
        # No recipient address in the message: this lands in application logs,
        # and an email address is PHI for a client and identifying for staff.
        # The user_id is enough to trace the account without storing the
        # identifier itself.
        logger.exception("Failed to send OTP email for user_id=%s", user_id)
        # The row keeps its ticket_hash, but the caller raises before setting
        # the cookie, so nobody holds the ticket and the next request_otp for
        # this user rotates it away.
        raise HTTPException(
            status_code=502,
            detail="Failed to send verification email. Please try again in a moment.",
        )

    return expires_at, ticket


async def verify_otp(
    db: AsyncSession, ticket: str | None, purpose: str, code: str
) -> OtpCode:
    """Check `code` against the OTP the ticket owns; return the row on success.

    The returned row carries `.user` (with `.role`) already loaded — that user,
    and only that user, is who the caller may create a session for. Deriving
    the identity from anywhere else in the request reopens the bypass this
    module was written to close.
    """
    now = datetime.now(timezone.utc)
    otp = await resolve_ticket(db, ticket, purpose)

    if otp.expires_at < now:
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new code.")

    # Checked before comparing, so a row that somehow arrives already at the
    # cap cannot be probed one more time.
    if otp.attempts >= MAX_VERIFY_ATTEMPTS:
        otp.consumed = True
        otp.ticket_hash = None
        await db.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many incorrect codes. Please sign in again to get a new one.",
        )

    if not verify_password(code, otp.code_hash):
        otp.attempts += 1
        remaining = MAX_VERIFY_ATTEMPTS - otp.attempts
        if remaining <= 0:
            # Burn the whole login attempt, not just the guess. Anyone who
            # wants another code has to present the password again.
            otp.consumed = True
            otp.ticket_hash = None
            await db.commit()
            raise HTTPException(
                status_code=429,
                detail="Too many incorrect codes. Please sign in again to get a new one.",
            )
        await db.commit()
        raise HTTPException(
            status_code=400,
            detail=f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} remaining.",
        )

    # Single use, and the ticket dies with it: consumed=True already makes the
    # row unmatchable (resolve_ticket filters on it), and nulling the hash
    # means a copy of the ticket captured in transit is inert immediately
    # rather than merely rejected.
    otp.consumed = True
    otp.ticket_hash = None
    await db.commit()
    return otp
