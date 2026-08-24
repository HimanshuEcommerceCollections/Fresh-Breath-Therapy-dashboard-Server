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
    # OPTIONAL, and never a lookup key. The account is resolved from the
    # login-ticket cookie; if an address is supplied it is cross-checked
    # against that and nothing more.
    #
    # Optional because the client no longer knows it: the address used to
    # travel in the /verify-otp URL, which put it into platform access logs and
    # browser history (audit item 4.4). The OTP page now asks
    # GET /api/auth/pending-login for a MASKED version to display, so it has
    # nothing to send back — and it does not need to, because the ticket
    # already decided whose login this is.
    email: Email | None = None
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


class PendingLoginResponse(BaseModel):
    """What the OTP screen shows, with nothing worth leaking in it."""

    # "k*****@fbtclinic.com" — recognisable, not disclosable.
    email_masked: str
    expires_at: datetime
    purpose: str
