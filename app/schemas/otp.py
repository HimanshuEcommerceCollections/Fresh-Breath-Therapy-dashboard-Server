from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.fields import Email


class OtpRequestResponse(BaseModel):
    detail: str = "OTP sent"
    expires_at: datetime | None = None
    otp_required: bool = True


class VerifyOtpRequest(BaseModel):
    email: Email
    code: str


class VerifyOtpResponse(BaseModel):
    detail: str = "Verified"