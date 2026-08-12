import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.services.cloudinary_service import upload_avatar
from app.services.pto_service import get_pto_balances_by_therapist, get_ytd_completed_sessions_by_therapist
from app.database import get_db
from app.models.therapist import Therapist
from app.models.location import Location
from app.models.client import Client
from app.models.payment import Payment
from app.models.enums import ClientStatus
from app.schemas.therapist import TherapistCreate, TherapistUpdate, TherapistResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/therapists", tags=["therapists"])

ACTIVE_CLIENT_STATUSES = (ClientStatus.THERAPY_SESSION_BOOKED, ClientStatus.ONGOING_THERAPY)


async def _attach_computed_fields(db: AsyncSession, therapists: list[Therapist]) -> list[TherapistResponse]:
    if not therapists:
        return []

    therapist_ids = [t.id for t in therapists]

    active_client_rows = (await db.execute(
        select(Client.therapist_id, func.count(Client.id))
        .where(Client.therapist_id.in_(therapist_ids), Client.status.in_(ACTIVE_CLIENT_STATUSES))
        .group_by(Client.therapist_id)
    )).all()
    active_clients_by_therapist = {row[0]: row[1] for row in active_client_rows}

    revenue_rows = (await db.execute(
        select(Client.therapist_id, func.coalesce(func.sum(Payment.amount_paid), 0))
        .join(Payment, Payment.client_id == Client.id)
        .where(Client.therapist_id.in_(therapist_ids))
        .group_by(Client.therapist_id)
    )).all()
    revenue_by_therapist = {row[0]: Decimal(str(row[1])) for row in revenue_rows}

    ytd_by_therapist = await get_ytd_completed_sessions_by_therapist(db)
    pto_balance_by_therapist = await get_pto_balances_by_therapist(db)

    responses = []
    for therapist in therapists:
        response = TherapistResponse.model_validate(therapist)
        response.active_client_count = active_clients_by_therapist.get(therapist.id, 0)
        response.revenue = revenue_by_therapist.get(therapist.id, Decimal("0"))
        response.ytd_sessions = ytd_by_therapist.get(therapist.id, 0)
        response.pto_balance = pto_balance_by_therapist.get(therapist.id, Decimal("0"))
        responses.append(response)
    return responses


@router.get("", response_model=list[TherapistResponse])
async def list_therapists(
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Therapist).options(selectinload(Therapist.location))
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    # Active therapists first, alphabetical within each group. An inactive
    # therapist is a record kept for history — it should never sit above
    # someone currently seeing clients just because the name sorts earlier.
    result = await db.execute(
        query.order_by(Therapist.is_active.desc(), Therapist.name)
    )
    therapists = result.scalars().all()
    return await _attach_computed_fields(db, therapists)


@router.get("/{therapist_id}", response_model=TherapistResponse)
async def get_therapist(
    therapist_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist_id)
    )
    therapist = result.scalar_one_or_none()
    if therapist is None:
        raise HTTPException(status_code=404, detail="Therapist not found")
    responses = await _attach_computed_fields(db, [therapist])
    return responses[0]


@router.post("", response_model=TherapistResponse, status_code=status.HTTP_201_CREATED)
async def create_therapist(
    payload: TherapistCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    location = await db.get(Location, payload.location_id)
    if location is None:
        raise HTTPException(status_code=400, detail="Location does not exist")

    therapist = Therapist(id=uuid.uuid4(), **payload.model_dump())

    # Best-effort reverse linking: if a user account already exists with this
    # email and isn't linked to another therapist record, link it now. Never
    # blocks creation, and never grants or changes the user's role.
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is not None:
        existing_link = await db.execute(
            select(Therapist).where(Therapist.user_id == user.id)
        )
        if existing_link.scalar_one_or_none() is None:
            therapist.user_id = user.id
            therapist.ever_linked = True

    db.add(therapist)
    await db.commit()

    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist.id)
    )
    return result.scalar_one()


@router.patch("/{therapist_id}", response_model=TherapistResponse)
async def update_therapist(
    therapist_id: uuid.UUID,
    payload: TherapistUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    therapist = await db.get(Therapist, therapist_id)
    if therapist is None:
        raise HTTPException(status_code=404, detail="Therapist not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "location_id" in update_data:
        location = await db.get(Location, update_data["location_id"])
        if location is None:
            raise HTTPException(status_code=400, detail="Location does not exist")

    for field, value in update_data.items():
        setattr(therapist, field, value)

    await db.commit()

    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist_id)
    )
    return result.scalar_one()


@router.delete("/{therapist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_therapist(
    therapist_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    therapist = await db.get(Therapist, therapist_id)
    if therapist is None:
        raise HTTPException(status_code=404, detail="Therapist not found")

    if therapist.ever_linked:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete a therapist record that has ever been linked to a user login — this preserves historical session/payment data",
        )

    await db.delete(therapist)
    await db.commit()

@router.post("/{therapist_id}/avatar", response_model=TherapistResponse)
async def upload_therapist_avatar(
    therapist_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    therapist = await db.get(Therapist, therapist_id)
    if therapist is None:
        raise HTTPException(status_code=404, detail="Therapist not found")

    url = await upload_avatar(file, folder="fbt/therapists")
    therapist.avatar_url = url
    await db.commit()

    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist_id)
    )
    return result.scalar_one()