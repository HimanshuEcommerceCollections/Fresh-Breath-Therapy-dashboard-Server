import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.base import ORMBase


class AuditLogResponse(ORMBase):
    """One audit record, as the log screen needs it.

    Everything here is safe to hand to an Admin: ids, field NAMES, and values
    only for non-identifying columns. No patient name, email, phone or note
    text can appear, by construction — see services/audit_registry.py.
    """

    id: uuid.UUID
    created_at: datetime

    actor_user_id: uuid.UUID | None = None
    actor_role: str | None = None
    actor_label: str | None = None

    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    entity_ids: list[str] | None = None
    entity_count: int | None = None
    truncated: bool = False

    changed_fields: dict | None = None
    criteria: dict | None = None

    source_ip: str | None = None
    user_agent: str | None = None
    route: str | None = None
    request_id: str | None = None

    outcome: str
    status_code: int | None = None


class AuditCount(BaseModel):
    """One bucket of a grouped count."""

    key: str
    count: int


class AuditLogSummary(BaseModel):
    """Headline numbers for a dashboard strip above the log table.

    Deliberately cheap: grouped counts over an indexed created_at range, so a
    screen can open on a summary without paging the whole table first.
    """

    total: int
    by_action: list[AuditCount]
    by_outcome: list[AuditCount]
    by_entity_type: list[AuditCount]
    distinct_actors: int
    oldest: datetime | None = None
    newest: datetime | None = None


class AuditActorOption(BaseModel):
    """A filter-dropdown entry, so the UI need not join users itself.

    actor_label is a snapshot taken when the action happened, which is why a
    deleted user still appears here — that is the point of not having a foreign
    key on actor_user_id.
    """

    actor_user_id: uuid.UUID | None = None
    actor_label: str | None = None
    actor_role: str | None = None
    event_count: int
