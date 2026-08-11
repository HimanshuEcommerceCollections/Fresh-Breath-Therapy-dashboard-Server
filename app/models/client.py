import uuid
from datetime import datetime
from sqlalchemy import String, Numeric, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base
from app.models.enums import ClientStatus


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Stable identity for spreadsheet-imported rows. The importer generates
    # it, then writes it back into the source sheet, so a later sync of the
    # same file recognises this row however it has been re-sorted since.
    # NULL for clients created in the dashboard. See models/import_batch.py.
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    # Nullable, unlike Lead.phone: clients converted before this column
    # existed have none on file, and a historical spreadsheet row may not
    # carry one either. Required-ness is a data-entry rule, not a schema one.
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    therapist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("therapists.id"), nullable=False
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("locations.id"), nullable=False
    )
    status: Mapped[ClientStatus] = mapped_column(
        Enum(
            ClientStatus,
            name="client_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=ClientStatus.CONSULTATION_COMPLETED,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    therapist: Mapped["Therapist"] = relationship()
    location: Mapped["Location"] = relationship()