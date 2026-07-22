import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge
from app.models.enums import PaymentStatus
from app.database import get_db
from app.models.payment import Payment
from app.models.client import Client
from app.models.package import Package
from app.schemas.payment import PaymentCreate, PaymentUpdate, PaymentResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin_or_coordinator
from app.dependencies.idempotency import idempotent

router = APIRouter(prefix="/api/payments", tags=["payments"])


def _payment_query():
    return select(Payment).options(selectinload(Payment.package))


def _to_response(payment: Payment) -> PaymentResponse:
    response = PaymentResponse.model_validate(payment)
    response.balance = Decimal(str(payment.due)) - Decimal(str(payment.paid))
    return response


@router.get("", response_model=list[PaymentResponse])
async def list_payments(
    client_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _payment_query()
    if client_id:
        query = query.where(Payment.client_id == client_id)
    result = await db.execute(query.order_by(Payment.date.desc()))
    return [_to_response(p) for p in result.scalars().all()]


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(_payment_query().where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _to_response(payment)


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
@idempotent(PaymentResponse, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    package = await db.get(Package, payload.package_id)
    if package is None:
        raise HTTPException(status_code=400, detail="Package does not exist")

    payment = Payment(id=uuid.uuid4(), **payload.model_dump())
    db.add(payment)
    await db.commit()

    result = await db.execute(_payment_query().where(Payment.id == payment.id))
    return _to_response(result.scalar_one())


@router.patch("/{payment_id}", response_model=PaymentResponse)
async def update_payment(
    payment_id: uuid.UUID,
    payload: PaymentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    update_data = payload.model_dump(exclude_unset=True)
    previously_overdue = payment.status == PaymentStatus.OVERDUE

    for field, value in update_data.items():
        setattr(payment, field, value)

    if payment.status == PaymentStatus.OVERDUE and not previously_overdue:
        client = await db.get(Client, payment.client_id)
        await create_notification(
            db, NotificationCategory.PAYMENT_DUE, NotificationBadge.OVERDUE,
            title="Payment overdue",
            body=f"Payment for {client.name if client else 'a client'} is overdue.",
            therapist_id=getattr(client, "therapist_id", None),
            related_entity_type="payment", related_entity_id=payment.id, commit=False,
        )

    await db.commit()
    result = await db.execute(_payment_query().where(Payment.id == payment_id))
    return _to_response(result.scalar_one())

@router.delete("/{payment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    payment = await db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    await db.delete(payment)
    await db.commit()