from fastapi import APIRouter, Header, HTTPException

from app.config import settings
from app.services.scheduler_service import run_notification_scan

router = APIRouter(prefix="/api/internal", tags=["internal"])


@router.get("/notification-scan")
async def trigger_notification_scan(authorization: str | None = Header(default=None)):
    """External trigger for the follow-up/appointment reminder scan — for
    platforms without a persistent process to run the in-app AsyncIOScheduler
    (see scheduler_service.py), e.g. a Vercel Cron Job hitting this route on
    the same 15-minute schedule. Fails closed: CRON_SECRET must be set and
    match, or every request is rejected."""
    if not settings.CRON_SECRET:
        raise HTTPException(status_code=503, detail="CRON_SECRET is not configured")
    if authorization != f"Bearer {settings.CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Invalid or missing credentials")

    await run_notification_scan()
    return {"status": "ok"}
