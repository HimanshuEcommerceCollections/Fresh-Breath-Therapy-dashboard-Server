import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, Boolean, Integer, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class OtpCode(Base):
    """One in-flight OTP, and the login attempt that owns it.

    This row is not just "a code we emailed". It is the server's record of a
    half-finished authentication: the credentials have been checked, the second
    factor has not. `ticket_hash` is what makes that binding real — see
    otp_service.py for why nothing may look this row up by email.
    """

    __tablename__ = "otp_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    purpose: Mapped[str] = mapped_column(String, nullable=False)  # "login" or "signup"
    # SHA-256 of the login ticket handed to the client once its password (or
    # Google identity) verified. The ONLY key this row may be found by, and
    # nulled the instant the OTP succeeds so a captured ticket dies with it.
    # NULL means "unreachable": pre-migration rows, and spent ones.
    ticket_hash: Mapped[str | None] = mapped_column(
        String, nullable=True, unique=True, index=True
    )
    # Wrong codes counted against this row, reset only when a NEW code is
    # issued. At MAX_VERIFY_ATTEMPTS the row is burned, so a 6-digit code can
    # never be ground down by brute force.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped["User"] = relationship()