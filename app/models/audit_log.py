"""The access log HIPAA actually asks for.

CloudTrail, Vercel logs and Supabase logs do not satisfy 164.312(b). They can
tell you a request happened; they cannot tell you WHICH CLIENT'S RECORD a named
user opened. Only the application knows that, so only the application can
record it.

Three design rules, each of which changes the schema:

APPEND ONLY. Nothing in this codebase updates or deletes a row here, with the
single exception of the retention purge (audit_service.purge_expired), which
may only delete by age and records its own entry when it does. If the person
who misused a record can also edit their line, the log is decoration.

NO FOREIGN KEY ON actor_user_id — deliberately, and this is not an oversight.
/api/auth/role-requests/{id} hard-deletes a user account. With a FK, CASCADE
would erase that user's entire history at the exact moment you most want it,
and SET NULL would anonymise it. The id is stored as a bare UUID and the role
and display name are SNAPSHOT alongside, so the trail survives the user row.

IDS AND FIELD NAMES, NEVER PATIENT VALUES (item 3.8). A log holding client
names and note contents is a second copy of the medical record in a table that
is usually less protected and never cleaned up. `changed_fields` therefore
records before/after only for non-identifying columns; for anything
identifying it records that the field changed and nothing more. See
audit_redaction.py for where that line is drawn.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Index, Integer, String, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# A user agent is the largest fixed field on the row and is attacker-supplied,
# so an uncapped one is both a storage cost on every request and a way to
# inflate this table on purpose.
MAX_USER_AGENT_LENGTH = 200

# Ceiling on how many ids one row will name. An unbounded export can return
# thousands; past this the row records the count and the filter with
# truncated=True, so a limit is always visible rather than silent.
MAX_ENTITY_IDS = 500


class AuditAction(str, enum.Enum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"
    LOGIN = "login"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    ACCESS_DENIED = "access_denied"
    # The retention purge auditing itself, so the one deleting path is visible.
    PURGE = "purge"


class AuditOutcome(str, enum.Enum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        # The default listing: newest first.
        Index("ix_audit_log_created_at", "created_at"),
        # "what did this user do" — the second most common investigation.
        Index("ix_audit_log_actor", "actor_user_id", "created_at"),
        # "who touched THIS client" — the first one anybody asks.
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
        # Pull every record belonging to one action.
        Index("ix_audit_log_request", "request_id"),
        # NOTE: entity_ids is deliberately NOT indexed. A GIN index over the
        # array would cost more than the data it indexes, and the query it
        # serves ("was this client in any result set") runs during an
        # investigation a couple of times a year, where a scan over a
        # date-filtered range is entirely acceptable.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ── who ───────────────────────────────────────────────────────────────
    # No FK: see the module docstring. Null for an unauthenticated attempt.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Snapshot, not a join — the role at the time of the action, which is the
    # question an investigator asks, and it may have changed since.
    actor_role: Mapped[str | None] = mapped_column(String, nullable=True)
    # Display label. Staff identity, never a patient's: "Priya S." or a
    # non-human origin such as "system:scheduler" / "system:webhook".
    actor_label: Mapped[str | None] = mapped_column(String, nullable=True)

    # ── what ──────────────────────────────────────────────────────────────
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    # The single record acted on, when there is one.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Every id a list/search/export returned. One row per REQUEST rather than
    # per record: same investigative fidelity at roughly an eighth of the
    # storage, and it keeps a busy afternoon of scrolling from becoming tens
    # of thousands of rows.
    entity_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    entity_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # {"amount_due": {"old": "100.00", "new": "50.00"}, "email": {"changed": true}}
    # Values for non-identifying columns; presence-only for identifying ones.
    changed_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Filters/range a list or export ran with, so "148 rows" is explainable.
    criteria: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── where from ────────────────────────────────────────────────────────
    source_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    route: Mapped[str | None] = mapped_column(String, nullable=True)
    # Ties every record produced by one HTTP request together.
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # ── how it went ───────────────────────────────────────────────────────
    outcome: Mapped[str] = mapped_column(String, nullable=False, default=AuditOutcome.SUCCESS.value)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
