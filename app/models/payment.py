import uuid
from datetime import datetime, date
from sqlalchemy import Numeric, String, Date, DateTime, ForeignKey, Enum, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import PaymentMethod, PaymentStatus


class Payment(Base):
    """What one session cost and whether it has been paid.

    Flat, by design. This used to be a ledger row against an Enrollment - a
    client's purchase-cycle of a Package - carrying a running balance, a
    denormalized package_id, and a status derived from how much of the package
    price had been settled. The practice does not sell packages: someone comes
    for therapy and pays for that session. Enrollments and packages are gone,
    and with them the running total, the balance_after snapshot and the
    derivation. A payment is now simply an amount, a method and a status.

    `amount` (was `amount_paid`) because a PENDING row records money expected,
    not money received - the old name contradicted the row it described.

    ONE PAYMENT PER SESSION, and never without one. session_id is NOT NULL and
    UNIQUE, and the pair is created in a single transaction by the scheduling
    endpoint. Two consequences worth knowing:

      - There is no client_id here. The person is whoever the session is for,
        which may be a lead. That also means converting a lead moves their
        payments for free: repointing the SESSION carries the payment with it.
      - ondelete="CASCADE": deleting a session takes its payment with it,
        because money recorded for an appointment that no longer exists is
        orphaned. The unique constraint is what makes that safe to reason
        about - there is only ever one row to remove.
    """

    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_date", "date"),
        Index("ix_payments_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See models/import_batch.py - proves a transaction has already been
    # imported and must not be added a second time on a re-sync.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    session: Mapped["Session"] = relationship(back_populates="payment")
