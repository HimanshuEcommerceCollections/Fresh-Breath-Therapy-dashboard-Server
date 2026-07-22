import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.notification import Notification, NotificationCategory, NotificationBadge
from app.models.feature_flag import FeatureFlag

# Maps each notification category to the feature_flags.key row that must be
# enabled for it to fire. VERIFY these key strings against your actual
# feature_flags rows before relying on this — they were never pasted back
# in the handoff doc.
CATEGORY_FLAG_KEY = {
    NotificationCategory.FOLLOW_UP_REMINDER: "follow_up_reminders",
    NotificationCategory.APPOINTMENT_REMINDER: "appointment_reminders",
    NotificationCategory.PAYMENT_DUE: "payment_due_alerts",
    NotificationCategory.MISSED_SESSION: "missed_session_alerts",
}


async def _category_enabled(db, category: NotificationCategory) -> bool:
    flag_key = CATEGORY_FLAG_KEY.get(category)
    if flag_key is None:
        # client_message has no toggle — always on
        return True
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.key == flag_key))
    flag = result.scalar_one_or_none()
    return flag is None or flag.enabled  # fail-open if the row is missing


async def create_notification(
    db,
    category: NotificationCategory,
    badge: NotificationBadge,
    title: str,
    body: str,
    therapist_id: uuid.UUID | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    commit: bool = True,
) -> Notification | None:
    """Creates a notification unless its category's flag is off, or an
    identical (category, badge, entity) notification already exists —
    the ON CONFLICT DO NOTHING is what makes the scheduled scan idempotent
    when it re-scans the same still-upcoming event every run."""

    if not await _category_enabled(db, category):
        return None

    stmt = (
        pg_insert(Notification)
        .values(
            id=uuid.uuid4(),
            category=category,
            badge=badge,
            title=title,
            body=body,
            therapist_id=therapist_id,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )
        .on_conflict_do_nothing(constraint="uq_notification_dedup")
        .returning(Notification.id)
    )
    result = await db.execute(stmt)
    new_id = result.scalar_one_or_none()

    if commit:
        await db.commit()

    if new_id is None:
        return None
    return await db.get(Notification, new_id)