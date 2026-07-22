import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.database import get_db
from app.models.client_message import ClientMessage
from app.models.client import Client
from app.models.user import User
from app.dependencies.auth import require_admin_or_coordinator
from app.schemas.client_message import ClientMessageCreate, ClientMessageResponse
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge

router = APIRouter(prefix="/api/client-messages", tags=["client-messages"])


@router.post("", response_model=ClientMessageResponse, status_code=201)
async def create_client_message(
    payload: ClientMessageCreate,
    db=Depends(get_db),
    current_user: User = Depends(require_admin_or_coordinator()),
):
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=400, detail="Client does not exist")

    message = ClientMessage(id=uuid.uuid4(), **payload.model_dump())
    db.add(message)
    await db.flush()

    await create_notification(
        db, NotificationCategory.CLIENT_MESSAGE, NotificationBadge.MESSAGE,
        title="New client message",
        body=f"{client.name} sent a message.",
        therapist_id=getattr(client, "therapist_id", None),
        related_entity_type="client_message", related_entity_id=message.id,
        commit=False,
    )
    await db.commit()
    await db.refresh(message)
    return message