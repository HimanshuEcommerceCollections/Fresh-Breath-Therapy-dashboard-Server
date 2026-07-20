import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.client import Client
from app.models.location import Location
from app.models.therapist import Therapist
from app.models.payment import Payment
from app.models.session import Session as SessionModel
from app.models.enums import ClientStatus
from app.schemas.client import ClientCreate, ClientUpdate, ClientResponse
from app.models.user import User
from app.dependencies.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/clients", tags=["clients"])


def _client_query():
    return select(Client).options(
        selectinload(Client.location),
        selectinload(Client.therapist).selectinload(Therapist.location),
    )


async def _attach_computed_fields(db: AsyncSession, clients: list[Client]) -> list[ClientResponse]:
    if not clients:
        return []

    client_ids = [c.id for c in clients]

    payment_rows = await db.execute(
        select(Payment.client_id, func.coalesce(func.sum(Payment.paid), 0))
        .where(Payment.client_id.in_(client_ids))
        .group_by(Payment.client_id)
    )
    lifetime_values = {row[0]: row[1] for row in payment_rows.all()}

    session_rows = await db.execute(
        select(SessionModel.client_id, func.count(SessionModel.id))
        .where(SessionModel.client_id.in_(client_ids))
        .group_by(SessionModel.client_id)
    )
    session_counts = {row[0]: row[1] for row in session_rows.all()}

    responses = []
    for client in clients:
        response = ClientResponse.model_validate(client)
        response.lifetime_value = Decimal(str(lifetime_values.get(client.id, 0)))
        response.sessions_count = session_counts.get(client.id, 0)
        responses.append(response)
    return responses


@router.get("", response_model=list[ClientResponse])
async def list_clients(
    status_filter: ClientStatus | None = None,
    location_id: uuid.UUID | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = _client_query()

    if status_filter:
        query = query.where(Client.status == status_filter)
    if location_id:
        query = query.where(Client.location_id == location_id)
    if search:
        term = f"%{search}%"
        query = query.where(or_(Client.name.ilike(term), Client.email.ilike(term)))

    result = await db.execute(query.order_by(Client.created_at.desc()))
    clients = result.scalars().all()
    return await _attach_computed_fields(db, clients)


@router.get("/{client_id}", response_model=ClientResponse)
async def get_client(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(_client_query().where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    responses = await _attach_computed_fields(db, [client])
    return responses[0]


@router.post("", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    location = await db.get(Location, payload.location_id)
    if location is None:
        raise HTTPException(status_code=400, detail="Location does not exist")

    therapist = await db.get(Therapist, payload.therapist_id)
    if therapist is None:
        raise HTTPException(status_code=400, detail="Therapist does not exist")

    client = Client(id=uuid.uuid4(), **payload.model_dump())
    db.add(client)
    await db.commit()

    result = await db.execute(_client_query().where(Client.id == client.id))
    saved = result.scalar_one()
    responses = await _attach_computed_fields(db, [saved])
    return responses[0]


@router.patch("/{client_id}", response_model=ClientResponse)
async def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "location_id" in update_data:
        location = await db.get(Location, update_data["location_id"])
        if location is None:
            raise HTTPException(status_code=400, detail="Location does not exist")

    if "therapist_id" in update_data:
        therapist = await db.get(Therapist, update_data["therapist_id"])
        if therapist is None:
            raise HTTPException(status_code=400, detail="Therapist does not exist")

    for field, value in update_data.items():
        setattr(client, field, value)

    await db.commit()

    result = await db.execute(_client_query().where(Client.id == client_id))
    saved = result.scalar_one()
    responses = await _attach_computed_fields(db, [saved])
    return responses[0]


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    client = await db.get(Client, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    await db.delete(client)
    await db.commit()