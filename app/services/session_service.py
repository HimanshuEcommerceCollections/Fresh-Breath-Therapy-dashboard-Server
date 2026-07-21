import uuid
from datetime import date as date_type, time as time_type
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.session import Session
from app.models.enums import SessionStatus


async def check_double_booking(
    db: AsyncSession,
    therapist_id: uuid.UUID,
    date: date_type,
    time: time_type,
    exclude_session_id: uuid.UUID | None = None,
):
    query = select(Session).where(
        Session.therapist_id == therapist_id,
        Session.date == date,
        Session.time == time,
        Session.status != SessionStatus.CANCELLED,
    )
    if exclude_session_id:
        query = query.where(Session.id != exclude_session_id)

    result = await db.execute(query)
    conflict = result.scalar_one_or_none()

    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "This therapist already has a session scheduled at this time.",
                "conflicting_session_id": str(conflict.id),
                "conflicting_date": str(conflict.date),
                "conflicting_time": str(conflict.time),
            },
        )