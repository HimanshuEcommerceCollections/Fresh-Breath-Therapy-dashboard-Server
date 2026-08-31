import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select, func
from sqlalchemy.orm import selectinload
from app.services.cloudinary_service import upload_avatar
from app.services.pto_service import get_pto_balances_by_therapist, get_ytd_completed_sessions_by_therapist
from app.database import get_db
from app.models.therapist import Therapist
from app.models.location import Location
from app.models.client import Client
from app.models.payment import Payment
from app.models.pto_transaction import PtoTransaction
from app.models.enums import ClientStatus
from app.schemas.therapist import TherapistCreate, TherapistUpdate, TherapistResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, get_own_therapist, require_admin
from app.services.audit_service import record_denied_on, record_read
from app.services.cloudinary_service import delete_avatar, upload_avatar

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
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    """The staff roster — scoped to the caller's own record for a Therapist.

    This used to return every therapist to every role: name, email, phone,
    credential, specialisation and employment status, to anyone who could log
    in. Minimum necessary (item 2.3) says a therapist needs their own caseload,
    not their colleagues' contact and employment details, and FBT confirmed
    that reading.

    Scoped rather than refused, deliberately. The sessions page builds its
    therapist filter from this endpoint and a Therapist can reach that page, so
    a 403 would break a screen they are entitled to use. One entry is also the
    honest answer for them: their session list is already filtered to
    themselves, so a filter offering anyone else was never meaningful.
    """
    query = select(Therapist).options(selectinload(Therapist.location))
    if current_user.role.name == "Therapist":
        if own_therapist is None:
            raise HTTPException(status_code=403, detail="No therapist record linked to this account")
        query = query.where(Therapist.id == own_therapist.id)
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    # Active therapists first, alphabetical within each group. An inactive
    # therapist is a record kept for history — it should never sit above
    # someone currently seeing clients just because the name sorts earlier.
    result = await db.execute(
        query.order_by(Therapist.is_active.desc(), Therapist.name)
    )
    therapists = result.scalars().all()
    responses = await _attach_computed_fields(db, therapists)
    # Unpaginated: every therapist record in one response, so the whole staff
    # roster is one read event.
    await record_read(db, "therapist", entity_ids=[t.id for t in therapists])
    return responses


@router.get("/{therapist_id}", response_model=TherapistResponse)
async def get_therapist(
    therapist_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist_id)
    )
    therapist = result.scalar_one_or_none()
    if therapist is None:
        raise HTTPException(status_code=404, detail="Therapist not found")

    # Same shape as the client/lead/session ownership checks: 404 rather than
    # 403, so the response does not confirm a record exists, and recorded
    # before raising because a 404 is invisible to the exception handler.
    if current_user.role.name == "Therapist" and (
        own_therapist is None or therapist.id != own_therapist.id
    ):
        await record_denied_on(db, "therapist", entity_id=therapist_id)
        raise HTTPException(status_code=404, detail="Therapist not found")
    responses = await _attach_computed_fields(db, [therapist])
    await record_read(db, "therapist", entity_id=therapist.id)
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

    # Drop their PTO ledger first. pto_transactions.therapist_id has no
    # ondelete, so Postgres RESTRICTs the delete while any row references the
    # therapist and the endpoint 500s on a raw foreign-key violation.
    #
    # This became reachable when the importer started accruing PTO for imported
    # completed sessions: previously a therapist only had ledger rows if
    # sessions had been completed through the dashboard, so a freshly imported
    # therapist had none and deleted cleanly. Now they do.
    #
    # Deleting is right rather than orphaning: the ledger is per-therapist and
    # means nothing without them, and this endpoint already refuses anyone ever
    # linked to a login — so whatever gets here is a record with no history
    # worth preserving.
    await db.execute(
        delete(PtoTransaction).where(PtoTransaction.therapist_id == therapist_id)
    )

    # Their photograph goes with the record. Nothing used to remove it, so a
    # deleted therapist's image stayed publicly readable in object storage
    # indefinitely (audit item 9.1). Captured BEFORE the delete, because the
    # attributes are gone afterwards.
    #
    # Storage first, then the row: delete_avatar never raises, so the worst case
    # is a logged orphan rather than a therapist who cannot be deleted because
    # their picture would not go away.
    storage_key, avatar_url = therapist.avatar_storage_key, therapist.avatar_url
    await delete_avatar(storage_key, avatar_url=avatar_url)

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

    # Remember what is being replaced before overwriting the columns.
    previous_key, previous_url = therapist.avatar_storage_key, therapist.avatar_url

    url, storage_key = await upload_avatar(file, folder="fbt/therapists")
    therapist.avatar_url = url
    therapist.avatar_storage_key = storage_key
    await db.commit()

    # AFTER the new one is safely stored and committed, not before: if this
    # order were reversed and the upload failed, the therapist would be left
    # with no photograph at all. Every re-upload previously orphaned the
    # previous image, so this leaked one file per edit.
    if previous_key or previous_url:
        await delete_avatar(previous_key, avatar_url=previous_url)

    result = await db.execute(
        select(Therapist).options(selectinload(Therapist.location)).where(Therapist.id == therapist_id)
    )
    return result.scalar_one()