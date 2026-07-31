import smtplib
from email.mime.text import MIMEText

from app.config import settings


def send_otp_email(to_email: str, code: str):
    """Blocking SMTP send. Callers on the async request path must run this
    via run_in_threadpool rather than calling it bare — see otp_service.py."""
    subject = "Your Fresh Breath Therapy verification code"
    body = f"Your verification code is: {code}\n\nThis code expires in 5 minutes."

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)
