import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.lead import Lead
from app.models.location import Location
from app.models.therapist import Therapist
from app.models.enums import LeadStatus
from app.schemas.lead import LeadCreate, LeadUpdate, LeadResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/leads", tags=["leads"])


def _lead_query():
    return select(Lead).options(
        selectinload(Lead.location),
        selectinload(Lead.therapist).selectinload(Therapist.location),
    )


@router.get("", response_model=list[LeadResponse])
async def list_leads(
    status_filter: LeadStatus | None = None,
    location_id: uuid.UUID | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _lead_query()

    if status_filter:
        query = query.where(Lead.status == status_filter)
    if location_id:
        query = query.where(Lead.location_id == location_id)
    if search:
        term = f"%{search}%"
        query = query.where(
            or_(Lead.name.ilike(term), Lead.email.ilike(term), Lead.phone.ilike(term))
        )

    result = await db.execute(query.order_by(Lead.created_at.desc()))
    return result.scalars().all()


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(_lead_query().where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    location = await db.get(Location, payload.location_id)
    if location is None:
        raise HTTPException(status_code=400, detail="Location does not exist")

    if payload.therapist_id:
        therapist = await db.get(Therapist, payload.therapist_id)
        if therapist is None:
            raise HTTPException(status_code=400, detail="Therapist does not exist")

    lead = Lead(id=uuid.uuid4(), **payload.model_dump())
    db.add(lead)
    await db.commit()

    result = await db.execute(_lead_query().where(Lead.id == lead.id))
    return result.scalar_one()


@router.patch("/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "location_id" in update_data:
        location = await db.get(Location, update_data["location_id"])
        if location is None:
            raise HTTPException(status_code=400, detail="Location does not exist")

    if "therapist_id" in update_data and update_data["therapist_id"] is not None:
        therapist = await db.get(Therapist, update_data["therapist_id"])
        if therapist is None:
            raise HTTPException(status_code=400, detail="Therapist does not exist")

    for field, value in update_data.items():
        setattr(lead, field, value)

    await db.commit()

    result = await db.execute(_lead_query().where(Lead.id == lead_id))
    return result.scalar_one()


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")

    await db.delete(lead)
    await db.commit()