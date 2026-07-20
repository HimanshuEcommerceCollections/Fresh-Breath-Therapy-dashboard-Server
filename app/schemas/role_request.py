import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.models.role_request import RoleRequestStatus


class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str


class SignupResponse(BaseModel):
    detail: str = "Account created. Awaiting admin approval."


class ApproveRoleRequest(BaseModel):
    role_id: uuid.UUID  # admin decides the role at approval time


class RoleRequestUserBrief(ORMBase):
    id: uuid.UUID
    name: str
    email: str


class RoleRequestRoleBrief(ORMBase):
    id: uuid.UUID
    name: str


class RoleRequestResponse(ORMBase):
    id: uuid.UUID
    status: RoleRequestStatus
    created_at: datetime
    reviewed_at: datetime | None
    user: RoleRequestUserBrief
    requested_role: RoleRequestRoleBrief | None