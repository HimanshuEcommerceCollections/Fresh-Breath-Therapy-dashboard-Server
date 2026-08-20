from datetime import datetime
from typing import Literal
from pydantic import BaseModel, EmailStr
from app.schemas.fields import Email
from app.schemas.user import UserResponse


class OtpRequestResponse(BaseModel):
    detail: str = "OTP sent"
    expires_at: datetime | None = None
    otp_required: bool = True


class VerifyOtpRequest(BaseModel):
    # NOT a lookup key. The account is resolved from the login-ticket cookie;
    # this is cross-checked against it and nothing more. See otp_service.py.
    email: Email
    code: str


class ResendOtpRequest(BaseModel):
    """Body of POST /api/auth/resend-otp.

    Was an untyped `dict` indexed with payload["purpose"], so a body missing
    either key raised KeyError and returned 500. `purpose` is constrained to
    the two values the OTP table actually stores.
    """
    purpose: Literal["login", "signup"]
    # Optional: the ticket already identifies the account, so this is only a
    # consistency check when the client sends it.
    email: Email | None = None


class VerifyOtpResponse(BaseModel):
    detail: str = "Verified"
    # Returned on the LOGIN flow so the client can seed its session cache and
    # navigate immediately. Without it the browser had to make a second,
    # serial call to /api/auth/me before it was allowed to leave the OTP
    # screen — the visible stall between "Login successful" and the dashboard.
    # None on the signup flow, which sets no session: the account is still
    # pending admin approval and there is no user to hand back.
    user: UserResponse | None = None