import uuid
from pydantic import BaseModel
from app.schemas.base import ORMBase
from app.models.enums import FeatureFlagCategory


class FeatureFlagUpdate(BaseModel):
    enabled: bool


class FeatureFlagResponse(ORMBase):
    id: uuid.UUID
    category: FeatureFlagCategory
    key: str
    label: str
    enabled: bool