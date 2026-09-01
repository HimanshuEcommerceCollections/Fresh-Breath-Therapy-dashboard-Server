import uuid
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.lead import Lead
from app.models.location import Location
from app.models.therapist import Therapist
from app.models.client import Client
from app.models.enums import LeadStatus
from app.schemas.lead import LeadCreate, LeadUpdate, LeadResponse
from app.schemas.client import ClientResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin, get_own_therapist
from app.services.audit_service import record_denied_on, record_read
from app.dependencies.idempotency import idempotent
from app.services.pagination import Page, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, apply_keyset_pagination, paginate_rows
from app.routers.clients import _client_query, _attach_computed_fields

router = APIRouter(prefix="/api/leads", tags=["leads"])


def _lead_query():
    return select(Lead).options(
        selectinload(Lead.location),
        selectinload(Lead.therapist).selectinload(Therapist.location),
    )


@router.get("", response_model=Page[LeadResponse])
async def list_leads(
    status_filter: LeadStatus | None = None,
    location_id: uuid.UUID | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    query = _lead_query()

    if current_user.role.name == "Therapist":
        if own_therapist is None:
            raise HTTPException(status_code=403, detail="No therapist record linked to this account")
        query = query.where(Lead.therapist_id == own_therapist.id)
    if status_filter:
        query = query.where(Lead.status == status_filter)
    if location_id:
        query = query.where(Lead.location_id == location_id)
    if search:
        term = f"%{search}%"
        query = query.where(
            or_(Lead.name.ilike(term), Lead.email.ilike(term), Lead.phone.ilike(term))
        )

    query = apply_keyset_pagination(query, Lead, cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)
    await record_read(
        db, "lead",
        entity_ids=[i.id for i in items],
        criteria={
            "status": status_filter.value if status_filter else None,
            "location_id": str(location_id) if location_id else None,
            # Lead search also matches phone, so the term is doubly PHI.
            "searched": bool(search),
            "limit": limit,
            "paged": bool(cursor),
        },
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    own_therapist: Therapist | None = Depends(get_own_therapist),
):
    result = await db.execute(_lead_query().where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if current_user.role.name == "Therapist" and lead.therapist_id != own_therapist.id:
        await record_denied_on(db, "lead", entity_id=lead_id)
        raise HTTPException(status_code=404, detail="Lead not found")
    await record_read(db, "lead", entity_id=lead.id)
    return lead


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
@idempotent(LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    request: Request,
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


@router.post("/{lead_id}/convert", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
@idempotent(ClientResponse, status_code=status.HTTP_201_CREATED)
async def convert_lead(
    lead_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=400, detail="Lead does not exist")
    if lead.converted_client_id is not None:
        raise HTTPException(status_code=400, detail="Lead is already converted")
    if lead.therapist_id is None:
        raise HTTPException(status_code=400, detail="Lead has no therapist assigned")

    client = Client(
        id=uuid.uuid4(),
        name=lead.name,
        email=lead.email,
        # Carried across deliberately: the lead form is where the phone number
        # is collected, and before clients had this column, converting a lead
        # silently discarded the only number anyone had for them.
        phone=lead.phone,
        # The admin's note follows the person, not the record type — it is the
        # same standing fact about them either side of the conversion.
        note=lead.note,
        therapist_id=lead.therapist_id,
        location_id=lead.location_id,
    )
    db.add(client)
    lead.converted_client_id = client.id
    await db.commit()

    result = await db.execute(_client_query().where(Client.id == client.id))
    saved = result.scalar_one()
    responses = await _attach_computed_fields(db, [saved])
    return responses[0]