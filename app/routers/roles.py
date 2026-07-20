from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.role import Role
from app.schemas.role import RoleResponse
from app.models.user import User
from app.dependencies.auth import get_current_user

router = APIRouter(prefix="/api/settings/roles", tags=["settings"])

@router.get("", response_model=list[RoleResponse])
async def list_roles(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Role).order_by(Role.name))
    return result.scalars().all()