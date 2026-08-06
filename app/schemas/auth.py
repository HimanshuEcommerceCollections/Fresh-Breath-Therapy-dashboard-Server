from pydantic import BaseModel, EmailStr
from app.schemas.fields import Email


class LoginRequest(BaseModel):
    email: Email
    password: str


class LoginResponse(BaseModel):
    detail: str = "Login successful"