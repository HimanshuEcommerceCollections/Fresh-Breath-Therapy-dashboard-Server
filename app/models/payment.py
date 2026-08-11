import uuid
from datetime import datetime, date
from sqlalchemy import Numeric, String, Date, DateTime, ForeignKey, Enum, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import PaymentMethod


class Payment(Base):
    """Immutable ledger of individual payment transactions against an
    Enrollment. client_id/package_id are denormalized from the enrollment at
    insert time (never change afterward) so the hot-path queries — a
    client's payment history, a client's lifetime value, revenue-by-client
    joins in therapists/dashboard — filter this table directly instead of
    joining through enrollments every time. enrollment_id is still the
    source of truth for the running total_paid/amount_due/status."""

    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_enrollment_date", "enrollment_id", "date"),
        Index("ix_payments_client_package", "client_id", "package_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See models/import_batch.py. The ledger is append-only, so on a re-sync
    # this is what proves a transaction has already been imported and must
    # not be added a second time.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("enrollments.id"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("packages.id"), nullable=False
    )
    amount_paid: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # The enrollment's amount_due immediately after this payment was applied
    # — a point-in-time fact for history display, never recomputed later.
    balance_after: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    enrollment: Mapped["Enrollment"] = relationship()
    client: Mapped["Client"] = relationship()
    package: Mapped["Package"] = relationship()
