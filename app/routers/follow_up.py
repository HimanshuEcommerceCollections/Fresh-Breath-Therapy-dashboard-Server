import uuid
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge
from app.database import get_db
from app.models.follow_up import FollowUp
from app.models.client import Client
from app.schemas.follow_up import (
    FollowUpCreate,
    FollowUpUpdate,
    FollowUpResponse,
    FollowUpStats,
    FollowUpStatus,
)
from app.models.user import User
from app.models.therapist import Therapist
from app.dependencies.auth import get_current_user, require_admin_or_coordinator, get_own_therapist

router = APIRouter(prefix="/api/follow-ups", tags=["follow-ups"])

def _compute_status(follow_up: FollowUp) -> FollowUpStatus:
    if follow_up.completed_at is not None:
        return FollowUpStatus.COMPLETED
    if follow_up.due_date < date.today():
        return FollowUpStatus.OVERDUE
    return FollowUpStatus.PENDING


def _to_response(follow_up: FollowUp) -> FollowUpResponse:
    response = FollowUpResponse.model_validate(follow_up)
    response.status = _compute_status(follow_up)
    return response


@router.get("", response_model=list[FollowUpResponse])
async def list_follow_ups(
    status_filter: FollowUpStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = select(FollowUp).order_by(FollowUp.due_date)
    if current_user.role.name == "Therapist":
        query = query.where(
            FollowUp.client_id.in_(
                select(Client.id).where(Client.therapist_id == own_therapist.id)
            )
        )
    result = await db.execute(query)
    follow_ups = result.scalars().all()
    responses = [_to_response(f) for f in follow_ups]

    if status_filter:
        responses = [r for r in responses if r.status == status_filter]

    return responses


@router.get("/stats", response_model=FollowUpStats)
async def get_follow_up_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = select(FollowUp)
    if current_user.role.name == "Therapist":
        if own_therapist is None:
            raise HTTPException(status_code=403, detail="No therapist record linked to this account")
        query = query.where(
            FollowUp.client_id.in_(
                select(Client.id).where(Client.therapist_id == own_therapist.id)
            )
        )
    result = await db.execute(query)
    follow_ups = result.scalars().all()

    stats = {"pending": 0, "overdue": 0, "completed": 0}
    for f in follow_ups:
        stats[_compute_status(f).value] += 1

    return FollowUpStats(**stats)

@router.get("/{follow_up_id}", response_model=FollowUpResponse)
async def get_follow_up(
    follow_up_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    follow_up = await db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")
    if current_user.role.name == "Therapist":
        client = await db.get(Client, follow_up.client_id)
        if client is None or client.therapist_id != own_therapist.id:
            raise HTTPException(status_code=404, detail="Follow-up not found")
    return _to_response(follow_up)


@router.post("", response_model=FollowUpResponse, status_code=status.HTTP_201_CREATED)
async def create_follow_up(
    payload: FollowUpCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    follow_up = FollowUp(id=uuid.uuid4(), **payload.model_dump())
    db.add(follow_up)
    if getattr(follow_up, "reminder", False):
        await create_notification(
            db, NotificationCategory.FOLLOW_UP_REMINDER, NotificationBadge.SCHEDULED,
            title="Reminder scheduled",
            body=f"Reminder set for {client.name}'s follow-up.",
            therapist_id=getattr(client, "therapist_id", None),
            related_entity_type="follow_up", related_entity_id=follow_up.id, commit=False,
        )
    await db.commit()
    await db.refresh(follow_up)
    return _to_response(follow_up)


@router.patch("/{follow_up_id}", response_model=FollowUpResponse)
async def update_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    follow_up = await db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(follow_up, field, value)
    
    if "completed_at" in payload.model_dump(exclude_unset=True) and follow_up.completed_at is not None:
        client = await db.get(Client, follow_up.client_id)
        await create_notification(
            db, NotificationCategory.FOLLOW_UP_REMINDER, NotificationBadge.COMPLETED,
            title="Follow-up completed",
            body=f"Follow-up with {client.name if client else 'a client'} was marked as completed.",
            therapist_id=getattr(client, "therapist_id", None),
            related_entity_type="follow_up", related_entity_id=follow_up.id, commit=False,
        )

    await db.commit()
    await db.refresh(follow_up)
    return _to_response(follow_up)


@router.post("/{follow_up_id}/complete", response_model=FollowUpResponse)
async def complete_follow_up(
    follow_up_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    follow_up = await db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    follow_up.completed_at = datetime.now(timezone.utc)
    client = await db.get(Client, follow_up.client_id)
    await create_notification(
        db, NotificationCategory.FOLLOW_UP_REMINDER, NotificationBadge.COMPLETED,
        title="Follow-up completed",
        body=f"Follow-up with {client.name if client else 'a client'} was marked as completed.",
        therapist_id=getattr(client, "therapist_id", None),
        related_entity_type="follow_up", related_entity_id=follow_up.id, commit=False,
    )
    await db.commit()
    await db.refresh(follow_up)
    return _to_response(follow_up)


@router.delete("/{follow_up_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_follow_up(
    follow_up_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    follow_up = await db.get(FollowUp, follow_up_id)
    if follow_up is None:
        raise HTTPException(status_code=404, detail="Follow-up not found")

    await db.delete(follow_up)
    await db.commit()