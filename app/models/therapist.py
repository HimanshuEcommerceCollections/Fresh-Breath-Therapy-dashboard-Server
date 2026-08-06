import uuid
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Therapist(Base):
    __tablename__ = "therapists"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    credential: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free text, e.g. "Anxiety, CBT" — the Add/Edit Therapist form has always
    # collected this; it previously had nowhere to go and was dropped on submit.
    specialization: Mapped[str | None] = mapped_column(String, nullable=True)
    # Full-time / Part-time / Contractor. Distinct from is_active: that's
    # whether they currently work here at all, this is on what terms. Kept a
    # plain string rather than a PG enum so adding "Intern" later needs no
    # migration; the form constrains the choices.
    employment_status: Mapped[str | None] = mapped_column(String, nullable=True)
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    ever_linked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    location: Mapped["Location"] = relationship()