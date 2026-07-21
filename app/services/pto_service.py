import uuid
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.pto_transaction import PtoTransaction
from app.models.enums import PtoTransactionType

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