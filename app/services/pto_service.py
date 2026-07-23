import uuid
from datetime import date
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.models.pto_transaction import PtoTransaction
from app.models.session import Session as SessionModel
from app.models.enums import PtoTransactionType, SessionStatus

ACCRUAL_RATE = Decimal("0.04")


async def accrue_pto_for_completed_session(db: AsyncSession, session_id: uuid.UUID, therapist_id: uuid.UUID):
    """Never deletes or reverses. If a session is later un-completed, this
    transaction stays permanently — it's a ledger, not a mutable flag."""
    existing = await db.execute(
        select(PtoTransaction).where(PtoTransaction.source_session_id == session_id)
    )
    if existing.scalar_one_or_none() is not None:
        return  # already accrued for this session, never double-accrue

    db.add(PtoTransaction(
        id=uuid.uuid4(),
        therapist_id=therapist_id,
        type=PtoTransactionType.ACCRUAL,
        hours=ACCRUAL_RATE,
        rate_applied=ACCRUAL_RATE,
        source_session_id=session_id,
    ))


async def get_pto_totals_by_therapist(db: AsyncSession) -> dict[uuid.UUID, dict[PtoTransactionType, Decimal]]:
    """Accrued and used PTO hours per therapist, one grouped query."""
    rows = (await db.execute(
        select(PtoTransaction.therapist_id, PtoTransaction.type, func.coalesce(func.sum(PtoTransaction.hours), 0))
        .group_by(PtoTransaction.therapist_id, PtoTransaction.type)
    )).all()

    per_therapist: dict[uuid.UUID, dict] = {}
    for therapist_id, ptype, hours in rows:
        per_therapist.setdefault(therapist_id, {})[ptype] = Decimal(str(hours))
    return per_therapist


async def get_pto_balances_by_therapist(db: AsyncSession) -> dict[uuid.UUID, Decimal]:
    """Accrued-minus-used PTO balance per therapist, one grouped query."""
    totals = await get_pto_totals_by_therapist(db)
    return {
        therapist_id: t.get(PtoTransactionType.ACCRUAL, Decimal("0")) - t.get(PtoTransactionType.USAGE, Decimal("0"))
        for therapist_id, t in totals.items()
    }


async def get_ytd_completed_sessions_by_therapist(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Completed session count since Jan 1 of the current year, per therapist."""
    year_start = date(date.today().year, 1, 1)
    rows = (await db.execute(
        select(SessionModel.therapist_id, func.count(SessionModel.id))
        .where(SessionModel.status == SessionStatus.COMPLETED, SessionModel.date >= year_start)
        .group_by(SessionModel.therapist_id)
    )).all()
    return {row[0]: row[1] for row in rows}