from datetime import datetime
from pydantic import BaseModel, EmailStr


class OtpRequestResponse(BaseModel):
    detail: str = "OTP sent"
    expires_at: datetime


class VerifyOtpRequest(BaseModel):
    email: EmailStr
    code: str


class VerifyOtpResponse(BaseModel):
    detail: str = "Verified"