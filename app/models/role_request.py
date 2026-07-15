import uuid
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.database import Base
from app.models.role import Role
from app.models.user import User

class RoleRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RoleRequest(Base):
    __tablename__ = "role_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    requested_role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("roles.id"), nullable=True)
    status: Mapped[RoleRequestStatus] = mapped_column(
        Enum(RoleRequestStatus, name="role_request_status",
             values_callable=lambda e: [x.value for x in e]),
        nullable=False, default=RoleRequestStatus.PENDING,
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    requested_role: Mapped["Role"] = relationship(foreign_keys=[requested_role_id])