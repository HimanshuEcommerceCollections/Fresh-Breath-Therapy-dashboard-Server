import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.location import Location
from app.models.therapist import Therapist
from app.models.client import Client
from app.models.lead import Lead
from app.schemas.location import LocationCreate, LocationResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/locations", tags=["locations"])


@router.get("", response_model=list[LocationResponse])
async def list_locations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Location).order_by(Location.name))
    return result.scalars().all()


@router.post("", response_model=LocationResponse, status_code=status.HTTP_201_CREATED)
async def create_location(
    payload: LocationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    existing = await db.execute(select(Location).where(Location.name == payload.name))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Location already exists")

    location = Location(id=uuid.uuid4(), name=payload.name)
    db.add(location)
    await db.commit()
    await db.refresh(location)
    return location


@router.delete("/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_location(
    location_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    location = await db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found")

    therapist_count = (await db.execute(
        select(func.count(Therapist.id)).where(Therapist.location_id == location_id)
    )).scalar_one()
    client_count = (await db.execute(
        select(func.count(Client.id)).where(Client.location_id == location_id)
    )).scalar_one()
    # leads.location_id is a NOT NULL FK — without this check, deleting a
    # location that a lead still points to (e.g. one the lead webhook created
    # automatically) would fail as an unhandled IntegrityError (500) instead
    # of this clear 400.
    lead_count = (await db.execute(
        select(func.count(Lead.id)).where(Lead.location_id == location_id)
    )).scalar_one()

    if therapist_count or client_count or lead_count:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot delete this location — it still has {therapist_count} therapist(s), "
                f"{client_count} client(s), and {lead_count} lead(s) assigned. Reassign them first."
            ),
        )

    await db.delete(location)
    await db.commit()