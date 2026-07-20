import uuid
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User
from app.models.therapist import Therapist
from app.services.jwt_service import decode_access_token

cookie_scheme = APIKeyCookie(name="access_token", auto_error=False)


async def get_current_user(
    access_token: str | None = Depends(cookie_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if access_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    user_id = decode_access_token(access_token)
    if user_id is None:
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
    if current_user.role.name != "Therapist":
        return None

    result = await db.execute(
        select(Therapist).where(Therapist.user_id == current_user.id)
    )
    therapist = result.scalar_one_or_none()

    if therapist is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No therapist record linked to this account",
        )

    return therapist