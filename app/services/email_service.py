import smtplib
from email.mime.text import MIMEText
from fastapi.concurrency import run_in_threadpool

from app.config import settings


def _send_sync(to_email: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


async def send_otp_email(to_email: str, code: str):
    subject = "Your Fresh Breath Therapy verification code"
    body = f"Your verification code is: {code}\n\nThis code expires in 5 minutes."
    await run_in_threadpool(_send_sync, to_email, subject, body)