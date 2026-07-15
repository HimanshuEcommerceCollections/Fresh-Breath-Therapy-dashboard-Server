import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.integration import Integration
from app.models.enums import IntegrationStatus
from app.schemas.integration import IntegrationConnect, IntegrationResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/settings/integrations", tags=["settings"])


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Integration).order_by(Integration.name))
    return result.scalars().all()


@router.post("/{integration_id}/connect", response_model=IntegrationResponse)
async def connect_integration(
    integration_id: uuid.UUID,
    payload: IntegrationConnect,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found")

    integration.status = IntegrationStatus.CONNECTED
    integration.credentials = payload.credentials
    integration.connected_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(integration)
    return integration


@router.post("/{integration_id}/disconnect", response_model=IntegrationResponse)
async def disconnect_integration(
    integration_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    integration = await db.get(Integration, integration_id)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found")

    integration.status = IntegrationStatus.AVAILABLE
    integration.credentials = None
    integration.connected_at = None
    await db.commit()
    await db.refresh(integration)
    return integration