import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Response, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import literal, select
from sqlalchemy.orm import joinedload, selectinload

from app.config import settings
from app.database import get_db
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.models.therapist import Therapist
from app.services.audit_context import set_actor
from app.services.jwt_service import (
    claim_as_datetime, create_access_token, decode_token_claims,
)
from app.services.auth_cookie import set_auth_cookie
from app.services.token_revocation_service import is_token_revoked

cookie_scheme = APIKeyCookie(name="access_token", auto_error=False)


SESSION_EXPIRED_DETAIL = "Session expired. Please sign in again."


async def get_current_user(
    response: Response,
    access_token: str | None = Depends(cookie_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    claims = decode_token_claims(access_token)
    if claims is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    jti = claims.get("jti")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    # One round trip for the whole dependency, not three.
    #
    # Every authenticated request pays this, so its cost is a floor under the
    # entire API. It used to be three sequential queries — the revoked-token
    # check, the user, then the role via selectinload — and against a database
    # a few hundred milliseconds away that was most of the ~2s that even
    # GET /api/auth/me was taking.
    #
    # joinedload rather than selectinload: role is a many-to-one, so it is a
    # single LEFT JOIN with no second SELECT. The revocation check rides along
    # as a correlated EXISTS, which is free on an indexed primary key and
    # cannot be a separate round trip.
    #
    # A logged-out token is still rejected even if unexpired and someone else
    # holds a copy — see token_revocation_service.
    revoked = (
        select(RevokedToken.jti).where(RevokedToken.jti == jti).exists()
        if jti else literal(False)
    )
    row = (await db.execute(
        select(User, revoked.label("is_revoked"))
        .options(joinedload(User.role))
        .where(User.id == user_id)
    )).first()

    if row is not None and row.is_revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user = row[0] if row is not None else None

    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")

    # ── sliding idle window, with an absolute ceiling ─────────────────────
    #
    # The token's own expiry IS the idle timeout. Re-issuing it while the user
    # is active means the session survives as long as they keep working and
    # dies ACCESS_TOKEN_EXPIRE_MINUTES after they stop — which is what
    # "automatic logoff after 30 minutes idle" actually means to a person.
    #
    # Doing it this way needs no `last_activity_at` column, and therefore no
    # database write on every request, and therefore no audit-log entry per
    # request per user. The token is the activity record.
    #
    # `sst` is the real sign-in time, carried across every re-issue, so a
    # session cannot slide indefinitely on a machine somebody walked away from
    # with a tab polling in the background.
    now = datetime.now(timezone.utc)
    session_started_at = claim_as_datetime(claims, "sst")

    if session_started_at is not None:
        if now - session_started_at > timedelta(hours=settings.SESSION_ABSOLUTE_HOURS):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=SESSION_EXPIRED_DETAIL
            )

    # Break-glass revocation. One timestamp invalidates every token issued
    # before it, on every device, which is what a stolen laptop or a departure
    # actually needs — logout only ever killed the single token that called it.
    if user.sessions_revoked_at is not None:
        token_issued_at = claim_as_datetime(claims, "iat") or session_started_at
        # A token we cannot date is refused once revocation is in force. Failing
        # closed matters more here than honouring an old token for a few
        # minutes: the whole point was to end every session.
        if token_issued_at is None or token_issued_at < user.sessions_revoked_at:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=SESSION_EXPIRED_DETAIL
            )

    if user.role_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending admin approval")

    # Re-issue only once the current token has some age on it, so an active user
    # collects a few cookies an hour rather than one per request. Deliberately
    # AFTER every rejection above: a token that should not be honoured must
    # never be renewed.
    #
    # Tokens minted before these claims existed have no `iat`; they are left
    # alone rather than slid, and expire inside the idle window on their own.
    issued_at = claim_as_datetime(claims, "iat")
    if issued_at is not None and (
        now - issued_at > timedelta(minutes=settings.TOKEN_REISSUE_AFTER_MINUTES)
    ):
        set_auth_cookie(
            response,
            create_access_token(user.id, session_started_at=session_started_at or now),
        )

    # Every authenticated request passes through here, so this is the one place
    # the audit layer needs to learn who is acting — no route has to remember.
    # The role is snapshot alongside the id because it is what an investigator
    # asks about and it may since have changed.
    set_actor(user.id, user.role.name if user.role else None, user.name)

    return user

def require_admin():
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.name != "Admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Admin can perform this action",
            )
        return current_user
    return checker


def require_admin_or_coordinator():
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.name not in ("Admin", "Coordinator"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only Admin or Coordinator can perform this action",
            )
        return current_user
    return checker


async def get_own_therapist(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Therapist | None:
    """Return the Therapist row linked to the current user, for any role.

    Only Therapist-role users REQUIRE a link (403 without one) — for them,
    callers row-filter down to this record. Admin/Coordinator may also have
    a linked record (an optional "my own" view) but are never row-filtered:
    gate filtering on current_user.role.name == "Therapist", not on this
    returning a value.
    """
    result = await db.execute(
        select(Therapist).where(Therapist.user_id == current_user.id)
    )
    therapist = result.scalar_one_or_none()

    if therapist is None and current_user.role.name == "Therapist":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No therapist record linked to this account",
        )

    return therapist