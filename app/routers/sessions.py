import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database import get_db
from app.models.session import Session
from app.models.client import Client
from app.models.therapist import Therapist
from app.models.enums import SessionStatus
from app.schemas.session import (
    SessionCreate, SessionUpdate, SessionSearchRequest, SessionResponse,
)
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin, get_own_therapist
from app.services.session_service import check_double_booking
from app.services.pto_service import accrue_pto_for_completed_session
from app.dependencies.idempotency import idempotent
from app.services.pagination import Page, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _session_query():
    return select(Session).options(
        selectinload(Session.client), selectinload(Session.therapist)
    )


@router.post("/search", response_model=Page[SessionResponse])
async def search_sessions(
    payload: SessionSearchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = _session_query()

    if current_user.role.name == "Therapist":
        if own_therapist is None:
            raise HTTPException(status_code=403, detail="No therapist record linked to this account")
        query = query.where(Session.therapist_id == own_therapist.id)
    elif payload.therapist_ids:
        query = query.where(Session.therapist_id.in_(payload.therapist_ids))

    if payload.client_id:
        query = query.where(Session.client_id == payload.client_id)
    if payload.status:
        query = query.where(Session.status == payload.status)
    if payload.date_from:
        query = query.where(Session.date >= payload.date_from)
    if payload.date_to:
        query = query.where(Session.date <= payload.date_to)

    limit = min(max(payload.limit, 1), MAX_PAGE_SIZE)
    query = apply_keyset_pagination(query, Session, payload.cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    result = await db.execute(_session_query().where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    if current_user.role.name == "Therapist":
        if own_therapist is None or session.therapist_id != own_therapist.id:
            raise HTTPException(status_code=404, detail="Session not found")

    return session


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
@idempotent(SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")
    therapist = await db.get(Therapist, payload.therapist_id)
    if therapist is None:
        raise HTTPException(status_code=400, detail="Therapist does not exist")

    await check_double_booking(db, payload.therapist_id, payload.date, payload.time)

    session = Session(id=uuid.uuid4(), **payload.model_dump())
    db.add(session)

    if session.status == SessionStatus.COMPLETED:
        await accrue_pto_for_completed_session(db, session.id, session.therapist_id)

    # A session can be entered already marked no-show (backfilling yesterday's
    # calendar), not only transitioned into it — notify either way, so this
    # matches the COMPLETED->PTO accrual above and the update path below.
    if session.status == SessionStatus.NO_SHOW:
        await create_notification(
            db, NotificationCategory.MISSED_SESSION, NotificationBadge.OVERDUE,
            title="Missed session", body="A session was marked as no-show.",
            therapist_id=session.therapist_id,
            related_entity_type="session", related_entity_id=session.id, commit=False,
        )

    await db.commit()

    result = await db.execute(_session_query().where(Session.id == session.id))
    return result.scalar_one()


@router.patch("/{session_id}", response_model=SessionResponse)
@idempotent(SessionResponse)
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    update_data = payload.model_dump(exclude_unset=True)

    new_date = update_data.get("date", session.date)
    new_time = update_data.get("time", session.time)
    if "date" in update_data or "time" in update_data:
        await check_double_booking(db, session.therapist_id, new_date, new_time, exclude_session_id=session.id)

    previously_completed = session.status == SessionStatus.COMPLETED

    for field, value in update_data.items():
        setattr(session, field, value)

    if session.status == SessionStatus.COMPLETED and not previously_completed:
        await accrue_pto_for_completed_session(db, session.id, session.therapist_id)

    if update_data.get("status") == SessionStatus.NO_SHOW:
        await create_notification(
            db, NotificationCategory.MISSED_SESSION, NotificationBadge.OVERDUE,
            title="Missed session", body="A session was marked as no-show.",
            therapist_id=session.therapist_id,
            related_entity_type="session", related_entity_id=session.id, commit=False,
        )

    await db.commit()

    result = await db.execute(_session_query().where(Session.id == session_id))
    return result.scalar_one()


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.delete(session)
    await db.commit()