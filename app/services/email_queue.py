import asyncio
import logging

logger = logging.getLogger(__name__)

# In-memory, single-process queue. Correct and sufficient for the current
# single Render instance, but does NOT coordinate across processes — if this
# service is ever scaled to multiple instances, each instance gets its own
# independent queue and worker, and the global one-at-a-time guarantee no
# longer holds across the fleet.
email_queue: asyncio.Queue = asyncio.Queue()
SEND_INTERVAL_SECONDS = 1.5  # spacing between sends, tune as needed

async def _email_worker():
    from app.services.email_service import send_otp_email
    while True:
        to_email, code = await email_queue.get()
        try:
            await send_otp_email(to_email, code)
        except Exception:
            logger.exception(f"Failed to send OTP email to {to_email}")
        finally:
            email_queue.task_done()
        await asyncio.sleep(SEND_INTERVAL_SECONDS)

def start_email_worker():
    asyncio.create_task(_email_worker())
