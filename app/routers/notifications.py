import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_, update

from app.database import get_db
from app.models.notification import Notification, NotificationCategory, NotificationBadge
from app.models.user import User
from app.models.therapist import Therapist
from app.dependencies.auth import get_current_user, get_own_therapist
from app.schemas.notification import NotificationResponse, NotificationSummaryResponse, NotificationTab

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

FOLLOW_UP_BADGES = [NotificationBadge.REMINDER, NotificationBadge.SCHEDULED,
                    NotificationBadge.COMPLETED]
ALERT_BADGES = [NotificationBadge.OVERDUE]


def _scope_query(query, current_user: User, own_therapist: Therapist | None):
    if current_user.role.name == "Therapist":
        if own_therapist is None:
            raise HTTPException(status_code=403, detail="No therapist record linked to this account")
        query = query.where(
            or_(Notification.therapist_id == own_therapist.id, Notification.therapist_id.is_(None))
        )
    return query


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    tab: NotificationTab = NotificationTab.ALL,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = select(Notification)
    query = _scope_query(query, current_user, own_therapist)

    if tab == NotificationTab.UNREAD:
        query = query.where(Notification.is_read.is_(False))
    elif tab == NotificationTab.READ:
        query = query.where(Notification.is_read.is_(True))
    elif tab == NotificationTab.FOLLOW_UP_REMINDERS:
        query = query.where(Notification.badge.in_(FOLLOW_UP_BADGES))
    elif tab == NotificationTab.ALERTS:
        query = query.where(Notification.badge.in_(ALERT_BADGES))

    result = await db.execute(query.order_by(Notification.created_at.desc()))
    return result.scalars().all()


@router.get("/summary", response_model=NotificationSummaryResponse)
async def notification_summary(
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    base = _scope_query(select(Notification), current_user, own_therapist)

    unread = (await db.execute(
        base.where(Notification.is_read.is_(False)).with_only_columns(func.count())
    )).scalar_one()
    follow_up = (await db.execute(
        base.where(Notification.badge.in_(FOLLOW_UP_BADGES)).with_only_columns(func.count())
    )).scalar_one()
    alerts = (await db.execute(
        base.where(Notification.badge.in_(ALERT_BADGES)).with_only_columns(func.count())
    )).scalar_one()

    return NotificationSummaryResponse(unread=unread, follow_up_reminders=follow_up, alerts=alerts)


@router.patch("/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: uuid.UUID,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = _scope_query(select(Notification).where(Notification.id == notification_id), current_user, own_therapist)
    result = await db.execute(query)
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    notification.is_read = True
    notification.read_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(notification)
    return notification


@router.post("/mark-all-read")
async def mark_all_read(
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    ids_query = _scope_query(
        select(Notification.id).where(Notification.is_read.is_(False)), current_user, own_therapist
    )
    ids = [row[0] for row in (await db.execute(ids_query)).all()]
    if ids:
        await db.execute(
            update(Notification)
            .where(Notification.id.in_(ids))
            .values(is_read=True, read_at=datetime.now(timezone.utc))
        )
        await db.commit()
    return {"marked_read": len(ids)}