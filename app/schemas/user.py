import uuid
from pydantic import BaseModel, EmailStr
from app.schemas.base import ORMBase
from app.schemas.role import RoleResponse
from app.schemas.fields import PersonName, Email


class UserCreate(BaseModel):
    name: PersonName
    email: Email
    password: str
    role_id: uuid.UUID


class UserUpdate(BaseModel):
    name: PersonName | None = None
    avatar_url: str | None = None
    is_active: bool | None = None


class UserResponse(ORMBase):
    id: uuid.UUID
    name: str
    email: str
    avatar_url: str | None
    is_active: bool
    role: RoleResponse