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
from app.schemas.fields import MAX_NAME_LENGTH
from app.schemas.webhook import LeadWebhookPayload, LeadWebhookResult
from app.services.notification_service import create_notification
from app.models.notification import NotificationCategory, NotificationBadge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/12345678", tags=["webhooks"])


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


async def _resolve_location(db: AsyncSession, submitted: str | None) -> tuple[uuid.UUID, bool]:
    """The automation sends a location NAME; leads.location_id is non-nullable.

    Matched case-insensitively against existing clinics first. If nothing
    matches — a genuinely new city, or just a spelling difference — a new
    Location is created on the fly rather than rejecting the lead or filing
    it under an unrelated default. The admin sees it appear in the location
    list (and every location dropdown already has a delete action), so a
    typo-driven duplicate costs one click to clean up; a rejected or
    misfiled lead costs a lost client.

    Returns (location_id, was_created).
    """
    if not submitted or not submitted.strip():
        submitted = "Unspecified"
    name = submitted.strip()

    result = await db.execute(
        select(Location).where(func.lower(Location.name) == func.lower(name))
    )
    location = result.scalar_one_or_none()
    if location is not None:
        return location.id, False

    if len(name) > MAX_NAME_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Submitted location name is longer than {MAX_NAME_LENGTH} characters "
                "and doesn't match an existing clinic."
            ),
        )

    new_location = Location(id=uuid.uuid4(), name=name)
    db.add(new_location)
    try:
        await db.flush()  # assigns nothing extra here, but surfaces the
        # unique-constraint violation now rather than at the caller's commit,
        # so the race below can be handled in the same request.
    except IntegrityError:
        # Two deliveries for the same brand-new city landed at once; the
        # second loses the race on the unique constraint — just use the
        # row the first one created.
        await db.rollback()
        result = await db.execute(
            select(Location).where(func.lower(Location.name) == func.lower(name))
        )
        location = result.scalar_one_or_none()
        if location is not None:
            return location.id, False
        raise
    return new_location.id, True


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

    location_id, location_created = await _resolve_location(db, payload.location)

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

    body = f"{payload.name} submitted the website form."
    if location_created:
        # Flag it in the notification itself — a new location silently
        # appearing in the dropdown is easy to miss; a bell notification with
        # the submitted name isn't.
        body += f' A new location, "{payload.location}", was added automatically.'
    await create_notification(
        db,
        NotificationCategory.CLIENT_MESSAGE,
        NotificationBadge.MESSAGE,
        title="New website enquiry",
        body=body,
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
    return LeadWebhookResult(
        status="created", lead_id=lead.id, duplicate=False,
        location_created=location_created,
    )
