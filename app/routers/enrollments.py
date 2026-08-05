import uuid
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.enrollment import Enrollment
from app.models.payment import Payment
from app.models.client import Client
from app.models.package import Package
from app.models.enums import EnrollmentStatus, PaymentStatus
from app.schemas.enrollment import (
    EnrollmentCreate,
    EnrollmentResponse,
    EnrollmentStatusUpdate,
    EnrollmentWithDetailsResponse,
)
from app.schemas.payment import PaymentResponse
from app.models.user import User
from app.dependencies.auth import require_admin, require_admin_or_coordinator
from app.dependencies.idempotency import idempotent
from app.services.pagination import (
    Page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows,
)

router = APIRouter(prefix="/api/enrollments", tags=["enrollments"])


def _enrollment_query():
    return select(Enrollment).options(
        selectinload(Enrollment.client), selectinload(Enrollment.package)
    )


def _payment_status_filter(query, status: PaymentStatus):
    """PAID/PARTIALLY_PAID/PENDING are derived rather than stored, so the
    filter has to be expressed against the money in SQL — doing it in Python
    after the LIMIT would under-fill pages (same reasoning as the follow-ups
    status filter)."""
    if status == PaymentStatus.OVERDUE:
        return query.where(Enrollment.is_overdue.is_(True))

    not_overdue = Enrollment.is_overdue.is_(False)
    if status == PaymentStatus.PENDING:
        return query.where(and_(not_overdue, Enrollment.total_paid <= 0))
    if status == PaymentStatus.PAID:
        return query.where(
            and_(not_overdue, Enrollment.total_paid >= Enrollment.package_price_snapshot)
        )
    return query.where(
        and_(
            not_overdue,
            Enrollment.total_paid > 0,
            Enrollment.total_paid < Enrollment.package_price_snapshot,
        )
    )


@router.get("", response_model=Page[EnrollmentWithDetailsResponse])
async def list_enrollments(
    status_filter: EnrollmentStatus | None = None,
    payment_status: PaymentStatus | None = None,
    client_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """The Payments table: one row per invoice (client + package purchase
    cycle), carrying the Due / Paid / Balance / Status the page renders. A
    flat payment ledger can't back that table — a ledger row has no
    'amount_due', and 'Pending (nothing paid)' can't exist on a row that by
    definition is money received."""
    query = _enrollment_query()
    if status_filter:
        query = query.where(Enrollment.status == status_filter)
    if payment_status:
        query = _payment_status_filter(query, payment_status)
    if client_id:
        query = query.where(Enrollment.client_id == client_id)

    query = apply_keyset_pagination(query, Enrollment, cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.post("", response_model=EnrollmentWithDetailsResponse, status_code=http_status.HTTP_201_CREATED)
@idempotent(EnrollmentWithDetailsResponse, status_code=http_status.HTTP_201_CREATED)
async def create_enrollment(
    payload: EnrollmentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Assign a package to a client without taking payment yet, so the
    invoice exists at Due = full price / Paid = 0 and shows as PENDING.
    Without this there is no way for an unpaid invoice to appear at all —
    enrollments would only ever spring into existence on first payment."""
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    package = await db.get(Package, payload.package_id)
    if package is None:
        raise HTTPException(status_code=400, detail="Package does not exist")

    # One active cycle per client+package (enforced by a partial unique index
    # too — checked here so the caller gets a clear message, not a DB error).
    existing = await db.execute(
        select(Enrollment).where(
            Enrollment.client_id == payload.client_id,
            Enrollment.package_id == payload.package_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="This client already has an active invoice for this package.",
        )

    enrollment = Enrollment(
        id=uuid.uuid4(),
        client_id=payload.client_id,
        package_id=payload.package_id,
        package_price_snapshot=package.price,
        total_paid=Decimal("0"),
        amount_due=package.price,
        status=EnrollmentStatus.ACTIVE,
        is_overdue=False,
    )
    db.add(enrollment)
    await db.commit()

    result = await db.execute(_enrollment_query().where(Enrollment.id == enrollment.id))
    return result.scalar_one()


@router.get("/history", response_model=list[EnrollmentResponse])
async def enrollment_history(
    client_id: uuid.UUID = Query(...),
    package_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """Every enrollment (any status) for this client+package, newest first.
    The Record Payment modal uses this — separately from /lookup, which only
    ever returns an ACTIVE one — to tell 'never enrolled before' apart from
    'the last cycle already completed', so it can show a 'starting a new
    cycle' notice specifically for the latter."""
    result = await db.execute(
        select(Enrollment)
        .where(Enrollment.client_id == client_id, Enrollment.package_id == package_id)
        .order_by(Enrollment.created_at.desc())
    )
    return result.scalars().all()


@router.get("/lookup", response_model=EnrollmentResponse)
async def lookup_active_enrollment(
    client_id: uuid.UUID = Query(...),
    package_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """The Record Payment modal calls this the moment both Client and
    Package are picked, to auto-populate 'paid so far' / 'amount due'
    instead of making the admin track or type either. 404 means there's no
    active cycle yet — the frontend treats that as amount_due = full package
    price, total_paid = 0 (a fresh first installment)."""
    result = await db.execute(
        select(Enrollment).where(
            Enrollment.client_id == client_id,
            Enrollment.package_id == package_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
    )
    enrollment = result.scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="No active enrollment for this client and package")
    return enrollment


@router.patch("/{enrollment_id}/status", response_model=EnrollmentWithDetailsResponse)
async def set_enrollment_payment_status(
    enrollment_id: uuid.UUID,
    payload: EnrollmentStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Only OVERDUE is stored. Picking PAID / PARTIALLY_PAID / PENDING clears
    the overdue flag and lets the status fall back to what the ledger says —
    which is how an admin undoes a mistaken 'Overdue'. It deliberately does
    NOT write the literal choice: marking a $0-paid invoice 'Paid' would put
    the badge at odds with the money, the revenue stats and the donut."""
    enrollment = await db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    enrollment.is_overdue = payload.status == PaymentStatus.OVERDUE
    await db.commit()

    result = await db.execute(_enrollment_query().where(Enrollment.id == enrollment_id))
    return result.scalar_one()


@router.delete("/{enrollment_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_enrollment(
    enrollment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Only an invoice with no payments against it can be removed — deleting
    one that has receipts would orphan ledger rows and silently drop revenue
    from every total."""
    enrollment = await db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    count = await db.execute(
        select(Payment.id).where(Payment.enrollment_id == enrollment_id).limit(1)
    )
    if count.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail="This invoice has payments recorded against it. Delete those payments first.",
        )

    await db.delete(enrollment)
    await db.commit()


@router.get("/{enrollment_id}/payments", response_model=list[PaymentResponse])
async def list_enrollment_payments(
    enrollment_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    enrollment = await db.get(Enrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    result = await db.execute(
        select(Payment)
        .options(selectinload(Payment.package), selectinload(Payment.enrollment))
        .where(Payment.enrollment_id == enrollment_id)
        .order_by(Payment.date.desc(), Payment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()
