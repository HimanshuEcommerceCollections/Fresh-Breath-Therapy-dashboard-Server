import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.payment import Payment
from app.models.client import Client
from app.models.enums import PaymentMethod, PaymentStatus
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentResponse
from app.models.user import User
from app.dependencies.auth import require_admin, require_admin_or_coordinator
from app.dependencies.idempotency import idempotent
from app.services.audit_service import record_read
from app.services.pagination import Page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows

router = APIRouter(prefix="/api/payments", tags=["payments"])


def _payment_query():
    return select(Payment).options(selectinload(Payment.client))


@router.get("", response_model=Page[PaymentResponse])
async def list_payments(
    client_id: uuid.UUID | None = None,
    status_filter: PaymentStatus | None = None,
    method: PaymentMethod | None = None,
    # Matched against the CLIENT's name. The admin looking for a payment knows
    # who it was for, not its id or its date.
    search: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    query = _payment_query()
    if client_id:
        query = query.where(Payment.client_id == client_id)
    if status_filter:
        query = query.where(Payment.status == status_filter)
    if method:
        query = query.where(Payment.method == method)
    if search:
        query = query.where(
            Payment.client_id.in_(
                select(Client.id).where(Client.name.ilike(f"%{search}%"))
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
            # The term is a client's name, so it is PHI and is not recorded.
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


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
@idempotent(PaymentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Records one payment.

    Almost everything this used to do is gone with enrollments: locking the
    active enrollment row FOR UPDATE, adding to a running total, deriving a
    status from the balance, opening a fresh purchase-cycle when the last one
    completed. A payment is now an amount against a client, so there is no
    shared row to contend over and nothing to recompute.
    """
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    payment = Payment(
        id=uuid.uuid4(),
        client_id=payload.client_id,
        amount=payload.amount,
        method=payload.method,
        status=payload.status,
        date=payload.date,
        created_by=current_user.id,
    )
    db.add(payment)
    await db.commit()

    result = await db.execute(_payment_query().where(Payment.id == payment.id))
    return result.scalar_one()


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
    """Deletes the row and nothing else.

    This was the most intricate handler in the file: removing a ledger row
    meant recomputing the enrollment's total, re-walking every later payment's
    balance_after, possibly reverting a completed cycle to active (and 409ing
    when a newer active cycle already existed), and dropping the enrollment
    entirely if it had no payments left. None of that has anything to recompute
    now.
    """
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    await db.delete(payment)
    await db.commit()
