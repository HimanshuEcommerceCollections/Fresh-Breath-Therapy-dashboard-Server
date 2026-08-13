import uuid
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import literal, select
from sqlalchemy.orm import joinedload, selectinload

from app.database import get_db
from app.models.revoked_token import RevokedToken
from app.models.user import User
from app.models.therapist import Therapist
from app.services.jwt_service import decode_token_claims
from app.services.token_revocation_service import is_token_revoked

cookie_scheme = APIKeyCookie(name="access_token", auto_error=False)


async def get_current_user(
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

    if user.role_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account pending admin approval")

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