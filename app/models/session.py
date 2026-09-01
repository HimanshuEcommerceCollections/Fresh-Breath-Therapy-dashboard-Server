import uuid
from datetime import datetime, date, time
from sqlalchemy import (
    String, Date, Time, DateTime, ForeignKey, Enum, CheckConstraint, Index, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import SessionType, SessionStatus


class Session(Base):
    """One appointment, for either a lead or a client.

    THE SUBJECT IS POLYMORPHIC. Exactly one of client_id / lead_id is set,
    enforced by a CHECK constraint rather than by convention — a session
    belonging to both or to neither is meaningless, and letting the database
    say so means no code path can create one by forgetting.

    It used to be client-only, which meant a lead could not be booked in until
    someone had converted them first. That is backwards: you book the
    consultation and the person becomes a client because of it.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "(client_id IS NOT NULL) <> (lead_id IS NOT NULL)",
            name="ck_sessions_one_subject",
        ),
        Index("ix_sessions_lead_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See models/import_batch.py — spreadsheet-import identity.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    # Nullable, and paired with lead_id below under ck_sessions_one_subject.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    therapist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("therapists.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    time: Mapped[time] = mapped_column(Time, nullable=False)
    type: Mapped[SessionType] = mapped_column(
        Enum(
            SessionType,
            name="session_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(
            SessionStatus,
            name="session_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=SessionStatus.SCHEDULED,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client | None"] = relationship()
    lead: Mapped["Lead | None"] = relationship()
    therapist: Mapped["Therapist"] = relationship()

    @property
    def subject(self):
        """The person this session is for, whichever kind they are.

        One accessor so callers never have to write `session.client or
        session.lead` and never have to remember which one is set.
        """
        return self.client if self.client_id is not None else self.lead

    @property
    def subject_kind(self) -> str:
        return "client" if self.client_id is not None else "lead"