import uuid
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
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

    # A logged-out token is rejected here even if it's still unexpired and
    # someone else still has a copy of it — see token_revocation_service.
    jti = claims.get("jti")
    if jti and await is_token_revoked(db, jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

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