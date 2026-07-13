from pydantic import BaseModel, ConfigDict


class ORMBase(BaseModel):
    """Base class for any schema that reads data out of a SQLAlchemy model."""
    model_config = ConfigDict(from_attributes=True)