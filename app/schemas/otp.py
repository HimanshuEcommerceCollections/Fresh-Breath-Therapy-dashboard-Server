from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.fields import Email
from app.schemas.user import UserResponse


class OtpRequestResponse(BaseModel):
    detail: str = "OTP sent"
    expires_at: datetime | None = None
    otp_required: bool = True


class VerifyOtpRequest(BaseModel):
    email: Email
    code: str


class VerifyOtpResponse(BaseModel):
    detail: str = "Verified"
    # Returned on the LOGIN flow so the client can seed its session cache and
    # navigate immediately. Without it the browser had to make a second,
    # serial call to /api/auth/me before it was allowed to leave the OTP
    # screen — the visible stall between "Login successful" and the dashboard.
    # None on the signup flow, which sets no session: the account is still
    # pending admin approval and there is no user to hand back.
    user: UserResponse | None = None