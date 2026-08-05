import uuid
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.payment import Payment
from app.models.enrollment import Enrollment
from app.models.client import Client
from app.models.package import Package
from app.models.enums import EnrollmentStatus
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentResponse, PaymentCreateResult
from app.schemas.enrollment import EnrollmentResponse
from app.models.user import User
from app.dependencies.auth import require_admin, require_admin_or_coordinator
from app.dependencies.idempotency import idempotent
from app.services.pagination import Page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows

router = APIRouter(prefix="/api/payments", tags=["payments"])


def _payment_query():
    return select(Payment).options(
        selectinload(Payment.package), selectinload(Payment.enrollment)
    )


@router.get("", response_model=Page[PaymentResponse])
async def list_payments(
    client_id: uuid.UUID | None = None,
    enrollment_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    query = _payment_query()
    if client_id:
        query = query.where(Payment.client_id == client_id)
    if enrollment_id:
        query = query.where(Payment.enrollment_id == enrollment_id)
    query = apply_keyset_pagination(query, Payment, cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)
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
    return payment


@router.post("", response_model=PaymentCreateResult, status_code=status.HTTP_201_CREATED)
@idempotent(PaymentCreateResult, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """The admin only ever types the amount for THIS installment — due/paid
    totals are entirely derived here, never entered by hand. Locks the
    client+package's active enrollment row (SELECT ... FOR UPDATE) for the
    duration of the update so two simultaneous payments can't both read the
    same total_paid and silently drop one of them."""
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    package = await db.get(Package, payload.package_id)
    if package is None:
        raise HTTPException(status_code=400, detail="Package does not exist")

    if payload.amount_paid <= 0:
        raise HTTPException(status_code=400, detail="Amount paid must be greater than 0")

    result = await db.execute(
        select(Enrollment)
        .where(
            Enrollment.client_id == payload.client_id,
            Enrollment.package_id == payload.package_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
        .with_for_update()
    )
    enrollment = result.scalar_one_or_none()
    is_new_cycle = enrollment is None

    if enrollment is None:
        # No active cycle (either never enrolled, or the last cycle for this
        # client+package already completed) — start a fresh one. A completed
        # enrollment is never reopened, so its history stays untouched.
        enrollment = Enrollment(
            id=uuid.uuid4(),
            client_id=payload.client_id,
            package_id=payload.package_id,
            package_price_snapshot=package.price,
            total_paid=Decimal("0"),
            amount_due=package.price,
            status=EnrollmentStatus.ACTIVE,
        )
        db.add(enrollment)
        await db.flush()  # assigns enrollment.id for the payment FK below

    enrollment.total_paid = Decimal(str(enrollment.total_paid)) + payload.amount_paid
    remaining = Decimal(str(enrollment.package_price_snapshot)) - Decimal(str(enrollment.total_paid))
    # Overpayment is accepted as-is in total_paid (for accurate history) but
    # amount_due never goes negative.
    enrollment.amount_due = remaining if remaining > 0 else Decimal("0")

    if Decimal(str(enrollment.total_paid)) >= Decimal(str(enrollment.package_price_snapshot)):
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.completed_at = datetime.now(timezone.utc)

    payment = Payment(
        id=uuid.uuid4(),
        enrollment_id=enrollment.id,
        client_id=payload.client_id,
        package_id=payload.package_id,
        amount_paid=payload.amount_paid,
        balance_after=enrollment.amount_due,
        method=payload.method,
        date=payload.date,
        created_by=current_user.id,
    )
    db.add(payment)

    await db.commit()

    result = await db.execute(_payment_query().where(Payment.id == payment.id))
    saved_payment = result.scalar_one()

    return PaymentCreateResult(
        payment=PaymentResponse.model_validate(saved_payment),
        enrollment=EnrollmentResponse.model_validate(enrollment),
        is_new_cycle=is_new_cycle,
    )


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
    """Deleting a ledger row isn't just removing the row — total_paid,
    amount_due, status, and every later payment's balance_after all have to
    be recomputed from whatever remains, or the ledger goes inconsistent."""
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    enrollment_result = await db.execute(
        select(Enrollment).where(Enrollment.id == payment.enrollment_id).with_for_update()
    )
    enrollment = enrollment_result.scalar_one()
    was_completed = enrollment.status == EnrollmentStatus.COMPLETED

    await db.delete(payment)
    await db.flush()

    remaining_result = await db.execute(
        select(Payment)
        .where(Payment.enrollment_id == enrollment.id)
        .order_by(Payment.date, Payment.created_at)
    )
    remaining_payments = remaining_result.scalars().all()

    running_total = Decimal("0")
    for p in remaining_payments:
        running_total += Decimal(str(p.amount_paid))
        remaining_due = Decimal(str(enrollment.package_price_snapshot)) - running_total
        p.balance_after = remaining_due if remaining_due > 0 else Decimal("0")

    price_snapshot = Decimal(str(enrollment.package_price_snapshot))
    enrollment.total_paid = running_total
    remaining_due = price_snapshot - running_total
    enrollment.amount_due = remaining_due if remaining_due > 0 else Decimal("0")
    will_be_completed = bool(remaining_payments) and running_total >= price_snapshot

    if was_completed and not will_be_completed:
        # Reverting completed -> active would violate "only one active
        # enrollment per client+package" if a newer cycle already exists.
        conflict = await db.execute(
            select(Enrollment.id).where(
                Enrollment.client_id == enrollment.client_id,
                Enrollment.package_id == enrollment.package_id,
                Enrollment.status == EnrollmentStatus.ACTIVE,
                Enrollment.id != enrollment.id,
            )
        )
        if conflict.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete this payment: a newer active cycle already exists for this client and package",
            )

    if will_be_completed:
        enrollment.status = EnrollmentStatus.COMPLETED
        if enrollment.completed_at is None:
            enrollment.completed_at = datetime.now(timezone.utc)
    else:
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.completed_at = None

    if not remaining_payments:
        # No payments left in this cycle at all — drop the empty enrollment
        # too, so it doesn't sit there blocking a future active cycle.
        await db.delete(enrollment)

    await db.commit()
