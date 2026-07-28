import resend
from fastapi.concurrency import run_in_threadpool

from app.config import settings

resend.api_key = settings.RESEND_API_KEY


def _send_sync(to_email: str, subject: str, text_body: str):
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": to_email,
        "subject": subject,
        "text": text_body,
    })


async def send_otp_email(to_email: str, code: str):
    subject = "Your Fresh Breath Therapy verification code"
    body = f"Your verification code is: {code}\n\nThis code expires in 5 minutes."
    await run_in_threadpool(_send_sync, to_email, subject, body)
