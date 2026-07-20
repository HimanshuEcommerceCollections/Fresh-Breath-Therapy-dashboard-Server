import uuid
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.services.cloudinary_service import upload_avatar
from app.database import get_db
from app.models.therapist import Therapist
from app.models.location import Location
from app.schemas.therapist import TherapistCreate, TherapistUpdate, TherapistResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/therapists", tags=["therapists"])


@router.get("", response_model=list[TherapistResponse])
async def list_therapists(
    location_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Therapist).options(selectinload(Therapist.location))
    if location_id:
        query = query.where(Therapist.location_id == location_id)
    result = await db.execute(query.order_by(Therapist.name))
    return result.scalars().all()


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
    return therapist


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