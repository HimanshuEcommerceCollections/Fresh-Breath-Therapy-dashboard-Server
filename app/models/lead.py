import uuid
from datetime import datetime
from sqlalchemy import String, Text, Integer, Boolean, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import LeadStatus


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender_or_pronoun: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    phone: Mapped[str] = mapped_column(String, nullable=False)
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id"), nullable=False
    )
    therapist_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("therapists.id"), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    # ── fields the public website form collects ──────────────────────────
    # The "Comment Or Message" box. Text, not String: people paste paragraphs.
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "Preferred Date & Time", stored EXACTLY as submitted rather than parsed.
    # This arrives from an external automation whose formatting we don't
    # control, and a parse failure on an inbound lead would mean either losing
    # the lead or losing the one detail the client actually asked for.
    preferred_datetime: Mapped[str | None] = mapped_column(String, nullable=True)
    # The website form's HIPAA/privacy consent checkbox. Worth persisting as
    # evidence of consent, not just as a gate on submission.
    consent_given: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The submission id from the upstream automation. Unique so a webhook retry
    # (automation platforms retry on timeout) can't create a duplicate lead;
    # nullable because leads added by hand in the dashboard have no such id,
    # and Postgres allows many NULLs under a UNIQUE constraint.
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    converted_client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )
    status: Mapped[LeadStatus] = mapped_column(
        Enum(
            LeadStatus,
            name="lead_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=LeadStatus.NEW_LEAD,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    location: Mapped["Location"] = relationship()
    therapist: Mapped["Therapist | None"] = relationship()