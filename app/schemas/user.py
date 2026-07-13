import uuid
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.role import RoleResponse


class UserCreate(BaseModel):
    name: str
    email: EmailStr
    password: str
    role_id: uuid.UUID


class UserUpdate(BaseModel):
    name: str | None = None
    avatar_url: str | None = None
    is_active: bool | None = None


class UserResponse(ORMBase):
    id: uuid.UUID
    name: str
    email: str
    avatar_url: str | None
    is_active: bool
    role: RoleResponse