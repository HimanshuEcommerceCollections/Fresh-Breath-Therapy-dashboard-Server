import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.enrollment import Enrollment
from app.models.payment import Payment
from app.models.enums import EnrollmentStatus
from app.schemas.enrollment import EnrollmentResponse, EnrollmentWithDetailsResponse
from app.schemas.payment import PaymentResponse
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator

router = APIRouter(prefix="/api/enrollments", tags=["enrollments"])


@router.get("", response_model=list[EnrollmentWithDetailsResponse])
async def list_enrollments(
    status_filter: EnrollmentStatus | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    """Every enrollment with its client/package — the source for the
    Payments page's revenue stats and status-distribution chart, computed
    from this list client-side rather than a flat payment ledger (a ledger
    row has no 'amount_due'; an enrollment does)."""
    query = select(Enrollment).options(
        selectinload(Enrollment.client), selectinload(Enrollment.package)
    )
    if status_filter:
        query = query.where(Enrollment.status == status_filter)
    result = await db.execute(query.order_by(Enrollment.created_at.desc()))
    return result.scalars().all()


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
