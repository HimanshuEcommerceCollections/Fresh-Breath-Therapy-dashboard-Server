import logging

# Resend email sending is suspended for now (EMAIL_SERVICE=false) — OTP is
# bypassed at the router level (see app/routers/auth.py), so this should
# never actually be called while suspended. Left commented out rather than
# deleted so re-enabling later is a quick uncomment, not a rewrite.
#
# import resend
# from fastapi.concurrency import run_in_threadpool
# from app.config import settings
#
# resend.api_key = settings.RESEND_API_KEY
#
#
# def _send_sync(to_email: str, subject: str, text_body: str):
#     resend.Emails.send({
#         "from": settings.RESEND_FROM_EMAIL,
#         "to": to_email,
#         "subject": subject,
#         "text": text_body,
#     })

logger = logging.getLogger(__name__)


async def send_otp_email(to_email: str, code: str):
    logger.warning(
        f"send_otp_email called while EMAIL_SERVICE is suspended — no email sent to {to_email}"
    )
