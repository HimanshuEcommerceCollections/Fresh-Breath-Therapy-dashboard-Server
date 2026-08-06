"""Inbound webhook: leads from the public website form.

Machine-to-machine, so it deliberately does NOT use the cookie/JWT auth every
other route uses — an automation platform has no session. It authenticates
with a shared secret in the X-Webhook-Secret header and fails closed: if
LEAD_WEBHOOK_SECRET is unset, every request is rejected rather than the
endpoint sitting open to anyone who learns the URL.

Flow: website form -> automation (saves to the spreadsheet) -> POST here ->
lead row in the dashboard.
"""
import hmac
import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import get_db
from app.models.lead import Lead
from app.models.location import Location
from app.models.enums import LeadStatus
from app.schemas.webhook import LeadWebhookPayload, LeadWebhookResult
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None, alias="X-Webhook-Secret"),
) -> None:
    if not settings.LEAD_WEBHOOK_SECRET:
        # Fail closed. An unconfigured secret must never mean "allow all".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Lead webhook is not configured",
        )
    if not x_webhook_secret or not hmac.compare_digest(
        x_webhook_secret, settings.LEAD_WEBHOOK_SECRET
    ):
        # compare_digest, not ==, so a wrong secret can't be recovered by
        # timing how long the comparison took.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing webhook secret",
        )


async def _resolve_location(db: AsyncSession, submitted: str | None) -> uuid.UUID:
    """The automation sends a location NAME; leads.location_id is non-nullable.
    Matched case-insensitively, then falls back to a configured default so a
    naming mismatch downgrades to "filed under the default clinic" rather than
    a dropped lead."""
    if submitted:
        result = await db.execute(
            select(Location).where(
                func.lower(Location.name) == func.lower(submitted.strip())
            )
        )
        location = result.scalar_one_or_none()
        if location is not None:
            return location.id

    if settings.LEAD_WEBHOOK_DEFAULT_LOCATION_ID:
        try:
            fallback = uuid.UUID(settings.LEAD_WEBHOOK_DEFAULT_LOCATION_ID)
        except ValueError:
            raise HTTPException(
                status_code=500,
                detail="LEAD_WEBHOOK_DEFAULT_LOCATION_ID is not a valid UUID",
            )
        if await db.get(Location, fallback) is not None:
            return fallback

    known = (await db.execute(select(Location.name).order_by(Location.name))).scalars().all()
    raise HTTPException(
        status_code=422,
        detail={
            "message": (
                "Could not match the submitted location to a clinic on record. "
                "Send one of the known names, or set "
                "LEAD_WEBHOOK_DEFAULT_LOCATION_ID."
            ),
            "submitted_location": submitted,
            "known_locations": list(known),
        },
    )


@router.post(
    "/leads",
    response_model=LeadWebhookResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_webhook_secret)],
)
async def receive_lead(
    payload: LeadWebhookPayload,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create a lead from a website submission.

    Idempotent on external_id: a redelivery (automation platforms retry on
    timeout or a 5xx) returns the lead already created rather than a duplicate.
    Without an external_id there is nothing to deduplicate on, so a retry WILL
    produce a second lead — which is the right trade if the alternative is
    guessing and dropping a genuine second enquiry from the same person.
    """
    if payload.external_id:
        existing = await db.execute(
            select(Lead).where(Lead.external_id == payload.external_id)
        )
        already = existing.scalar_one_or_none()
        if already is not None:
            # Nothing was created on a replay, so don't claim 201.
            response.status_code = status.HTTP_200_OK
            return LeadWebhookResult(
                status="already_received", lead_id=already.id, duplicate=True
            )

    location_id = await _resolve_location(db, payload.location)

    lead = Lead(
        id=uuid.uuid4(),
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        location_id=location_id,
        # No therapist yet — assignment is a human decision, and Lead.convert
        # already refuses to run without one.
        therapist_id=None,
        source=payload.source or "Website",
        message=payload.message,
        preferred_datetime=payload.preferred_datetime,
        consent_given=payload.consent_given,
        external_id=payload.external_id,
        status=LeadStatus.NEW_LEAD,
    )
    db.add(lead)

    await create_notification(
        db,
        NotificationCategory.CLIENT_MESSAGE,
        NotificationBadge.MESSAGE,
        title="New website enquiry",
        body=f"{payload.name} submitted the website form.",
        related_entity_type="lead",
        related_entity_id=lead.id,
        commit=False,
    )

    try:
        await db.commit()
    except IntegrityError:
        # Two deliveries of the same external_id racing each other: the unique
        # constraint is the real guard, the SELECT above is just the fast path.
        await db.rollback()
        existing = await db.execute(
            select(Lead).where(Lead.external_id == payload.external_id)
        )
        already = existing.scalar_one_or_none()
        if already is not None:
            response.status_code = status.HTTP_200_OK
            return LeadWebhookResult(
                status="already_received", lead_id=already.id, duplicate=True
            )
        raise

    logger.info("Lead received from website webhook: %s (%s)", lead.id, payload.email)
    return LeadWebhookResult(status="created", lead_id=lead.id, duplicate=False)
