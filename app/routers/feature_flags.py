import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.feature_flag import FeatureFlag
from app.models.enums import FeatureFlagCategory
from app.schemas.feature_flag import FeatureFlagUpdate, FeatureFlagResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/settings/feature-flags", tags=["settings"])


@router.get("", response_model=list[FeatureFlagResponse])
async def list_feature_flags(
    category: FeatureFlagCategory | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(FeatureFlag)
    if category:
        query = query.where(FeatureFlag.category == category)
    result = await db.execute(query.order_by(FeatureFlag.category, FeatureFlag.label))
    return result.scalars().all()


@router.patch("/{flag_id}", response_model=FeatureFlagResponse)
async def update_feature_flag(
    flag_id: uuid.UUID,
    payload: FeatureFlagUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    flag = await db.get(FeatureFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="Feature flag not found")

    flag.enabled = payload.enabled
    await db.commit()
    await db.refresh(flag)
    return flag