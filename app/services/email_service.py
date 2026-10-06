import smtplib
from datetime import date, time
from email.mime.text import MIMEText

from app.config import settings

CLINIC_NAME = "Fresh Breath Therapy"


def _send(to_email: str, subject: str, body: str) -> None:
    """Blocking SMTP send. Callers on the async request path must run this
    via run_in_threadpool rather than calling it bare — see otp_service.py."""
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as server:
        server.starttls()
        server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        server.send_message(msg)


def send_otp_email(to_email: str, code: str):
    _send(
        to_email,
        "Your Fresh Breath Therapy verification code",
        f"Your verification code is: {code}\n\nThis code expires in 5 minutes.",
    )


def _session_details(session_date: date, session_time: time, therapist_name: str) -> str:
    # Minimum necessary: when, and with whom. No session type, notes or
    # payment — an email is the least protected place this data will ever sit.
    when_date = session_date.strftime("%A, %B %d, %Y").replace(" 0", " ")
    when_time = session_time.strftime("%I:%M %p").lstrip("0")
    return (
        f"  Date:      {when_date}\n"
        f"  Time:      {when_time} (Eastern Time)\n"
        f"  Therapist: {therapist_name}\n"
    )


def send_session_scheduled_email(
    to_email: str, name: str, session_date: date, session_time: time, therapist_name: str
) -> None:
    _send(
        to_email,
        f"Your session with {CLINIC_NAME} is scheduled",
        f"Hi {name},\n\n"
        f"Your session has been scheduled:\n\n"
        f"{_session_details(session_date, session_time, therapist_name)}\n"
        f"If you need to reschedule, please contact us.\n\n"
        f"— {CLINIC_NAME}",
    )


def send_session_reminder_email(
    to_email: str, name: str, session_date: date, session_time: time, therapist_name: str
) -> None:
    _send(
        to_email,
        f"Reminder: your {CLINIC_NAME} session is tomorrow",
        f"Hi {name},\n\n"
        f"This is a reminder that you have a session in 24 hours:\n\n"
        f"{_session_details(session_date, session_time, therapist_name)}\n"
        f"If you can no longer attend, please let us know as soon as possible.\n\n"
        f"— {CLINIC_NAME}",
    )
