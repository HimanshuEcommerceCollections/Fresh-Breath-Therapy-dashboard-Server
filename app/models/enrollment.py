import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import Boolean, Numeric, String, DateTime, ForeignKey, Enum, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import EnrollmentStatus, PaymentStatus


class Enrollment(Base):
    """One client's purchase-cycle of one package. amount_due/total_paid are
    maintained by application logic in the payments router (see
    create_payment) — never edited directly by a client request.

    A partial unique index (see the migration) enforces at most one 'active'
    row per (client_id, package_id): completing an enrollment and starting a
    new cycle for the same client+package always inserts a fresh row rather
    than reopening the old one, so history stays intact.
    """

    __tablename__ = "enrollments"
    __table_args__ = (
        Index("ix_enrollments_client_package", "client_id", "package_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # See models/import_batch.py — spreadsheet-import identity.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    package_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("packages.id"), nullable=False
    )
    # Copied from package.price at creation time — an enrollment already in
    # progress must never move if the package's list price changes later.
    package_price_snapshot: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    total_paid: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    amount_due: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(
            EnrollmentStatus,
            name="enrollment_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=EnrollmentStatus.ACTIVE,
    )
    # Admin-only override. The other three payment statuses are derived from
    # the money (see payment_status); "overdue" is a human judgement call, so
    # it's the only one that needs storing. Clearing it re-derives.
    is_overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    client: Mapped["Client"] = relationship()
    package: Mapped["Package"] = relationship()

    @property
    def payment_status(self) -> PaymentStatus:
        """The status the Payments table renders. Admin's overdue flag wins;
        otherwise it falls out of what's actually been paid, so the badge can
        never claim 'Paid' on an invoice that still has a balance."""
        if self.is_overdue:
            return PaymentStatus.OVERDUE
        paid = Decimal(str(self.total_paid or 0))
        price = Decimal(str(self.package_price_snapshot or 0))
        if paid <= 0:
            return PaymentStatus.PENDING
        if paid >= price:
            return PaymentStatus.PAID
        return PaymentStatus.PARTIALLY_PAID
