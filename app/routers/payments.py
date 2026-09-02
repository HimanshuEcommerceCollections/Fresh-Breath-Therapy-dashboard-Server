import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.payment import Payment
from app.models.session import Session
from app.models.client import Client
from app.models.lead import Lead
from app.models.enums import PaymentMethod, PaymentStatus
from app.schemas.payment import PaymentUpdate, PaymentResponse
from app.models.user import User
from app.dependencies.auth import require_admin, require_admin_or_coordinator
from app.services.audit_service import record_read
from app.services.pagination import Page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows

router = APIRouter(prefix="/api/payments", tags=["payments"])


# NOTE: no POST. A payment cannot exist without a session, so it is created
# by POST /api/sessions in the same transaction (see routers/sessions.py).
# This router reads, edits and deletes.


def _payment_query():
    return select(Payment).options(
        selectinload(Payment.session).selectinload(Session.client),
        selectinload(Payment.session).selectinload(Session.lead),
    )


@router.get("", response_model=Page[PaymentResponse])
async def list_payments(
    client_id: uuid.UUID | None = None,
    status_filter: PaymentStatus | None = None,
    method: PaymentMethod | None = None,
    # Matched against the session subject's name — lead or client. The admin
    # looking for a payment knows who it was for, not its id or its date.
    search: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    query = _payment_query()
    if client_id:
        # Through the session — a payment has no client of its own.
        query = query.where(
            Payment.session_id.in_(
                select(Session.id).where(Session.client_id == client_id)
            )
        )
    if status_filter:
        query = query.where(Payment.status == status_filter)
    if method:
        query = query.where(Payment.method == method)
    if search:
        # Matched against the session subject's name, whichever kind they are.
        # A payment for a lead's consultation is as searchable as one for a
        # client's — the admin types a name, not a record type.
        term = f"%{search}%"
        query = query.where(
            Payment.session_id.in_(
                select(Session.id).where(
                    or_(
                        Session.client_id.in_(
                            select(Client.id).where(Client.name.ilike(term))
                        ),
                        Session.lead_id.in_(
                            select(Lead.id).where(Lead.name.ilike(term))
                        ),
                    )
                )
            )
        )

    query = apply_keyset_pagination(query, Payment, cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)
    await record_read(
        db, "payment",
        entity_ids=[i.id for i in items],
        criteria={
            "client_id": str(client_id) if client_id else None,
            "status": status_filter.value if status_filter else None,
            "method": method.value if method else None,
            # The term is a person's name, so it is PHI and is not recorded.
            "searched": bool(search),
            "limit": limit, "paged": bool(cursor),
        },
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    result = await db.execute(_payment_query().where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    await record_read(db, "payment", entity_id=payment.id)
    return payment


@router.patch("/{payment_id}", response_model=PaymentResponse)
async def update_payment(
    payment_id: uuid.UUID,
    payload: PaymentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(payment, field, value)

    await db.commit()
    result = await db.execute(_payment_query().where(Payment.id == payment_id))
    return result.scalar_one()


@router.delete("/{payment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Deletes the payment, leaving its session in place.

    Kept, even though a session always creates one, for the case where a
    payment was recorded in error and the appointment itself still stands.
    Deleting the SESSION removes its payment automatically (ON DELETE CASCADE
    plus delete-orphan), which is the usual direction.

    This was the most intricate handler in the file: removing a ledger row
    meant recomputing the enrollment's total, re-walking every later payment's
    balance_after, possibly reverting a completed cycle to active, and dropping
    the enrollment if it had no payments left. None of that exists now.
    """
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    await db.delete(payment)
    await db.commit()
