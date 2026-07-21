import uuid
from datetime import datetime, date
from sqlalchemy import Numeric, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy import Date, String
from app.models.therapist import Therapist
from app.models.session import Session
from app.database import Base
from app.models.enums import PtoTransactionType

class PtoTransaction(Base):
    __tablename__ = "pto_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    therapist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("therapists.id"), nullable=False
    )
    type: Mapped[PtoTransactionType] = mapped_column(
        Enum(
            PtoTransactionType,
            name="pto_transaction_type",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    hours: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    rate_applied: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    source_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    therapist: Mapped["Therapist"] = relationship()
    source_session: Mapped["Session | None"] = relationship()
    date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)