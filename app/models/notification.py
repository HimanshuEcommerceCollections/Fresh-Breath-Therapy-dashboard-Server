import uuid
from datetime import datetime
import enum

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Enum, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class NotificationCategory(str, enum.Enum):
    FOLLOW_UP_REMINDER = "follow_up_reminder"
    APPOINTMENT_REMINDER = "appointment_reminder"
    PAYMENT_DUE = "payment_due"
    MISSED_SESSION = "missed_session"
    CLIENT_MESSAGE = "client_message"


class NotificationBadge(str, enum.Enum):
    REMINDER = "reminder"
    OVERDUE = "overdue"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    MESSAGE = "message"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    category: Mapped[NotificationCategory] = mapped_column(
        Enum(NotificationCategory, name="notification_category",
             values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    badge: Mapped[NotificationBadge] = mapped_column(
        Enum(NotificationBadge, name="notification_badge",
             values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(String, nullable=False)

    # Null = org-wide notification, not tied to one therapist's own records
    therapist_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("therapists.id", ondelete="CASCADE"), nullable=True
    )

    # Lets the "View Follow-Up" / "View Message" button route to the right place
    related_entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    therapist: Mapped["Therapist | None"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "category", "badge", "related_entity_type", "related_entity_id",
            name="uq_notification_dedup",
        ),
    )