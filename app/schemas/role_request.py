import uuid
from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.models.role_request import RoleRequestStatus
from app.schemas.fields import PersonName, Email


class SignupRequest(BaseModel):
    name: PersonName
    email: Email
    password: str


class SignupResponse(BaseModel):
    detail: str = "Account created. Awaiting admin approval."


class ApproveRoleRequest(BaseModel):
    role_id: uuid.UUID  # admin decides the role at approval time


class ChangeRoleRequest(BaseModel):
    role_id: uuid.UUID  # the role an already-approved account moves to


class RoleRequestUserBrief(ORMBase):
    id: uuid.UUID
    name: str
    email: str


class RoleRequestRoleBrief(ORMBase):
    id: uuid.UUID
    name: str


class RoleRequestTherapistBrief(ORMBase):
    id: uuid.UUID
    name: str
    is_active: bool


class RoleRequestResponse(ORMBase):
    id: uuid.UUID
    status: RoleRequestStatus
    created_at: datetime
    reviewed_at: datetime | None
    user: RoleRequestUserBrief
    requested_role: RoleRequestRoleBrief | None
    # The therapist record linked to this account, if any. Shown so an admin
    # can see up front that revoking access is blocked while it is active.
    linked_therapist: RoleRequestTherapistBrief | None = None
