from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.organization_settings import OrganizationSettings
from app.schemas.organization_settings import OrganizationSettingsUpdate, OrganizationSettingsResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/settings/organization", tags=["settings"])


async def _get_or_create(db: AsyncSession) -> OrganizationSettings:
    result = await db.execute(select(OrganizationSettings).limit(1))
    org = result.scalar_one_or_none()
    if org is None:
        org = OrganizationSettings(
            org_name="Fresh Breath Therapy",
            primary_email="admin@freshbreath.co",
            timezone="America/New_York",
        )
        db.add(org)
        await db.commit()
        await db.refresh(org)
    return org


@router.get("", response_model=OrganizationSettingsResponse)
async def get_organization(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _get_or_create(db)


@router.patch("", response_model=OrganizationSettingsResponse)
async def update_organization(
    payload: OrganizationSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    org = await _get_or_create(db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value)
    await db.commit()
    await db.refresh(org)
    return org