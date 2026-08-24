"""Spreadsheet import: batch + per-row audit trail.

Every import is a two-phase operation. Phase one parses the uploaded sheet
into `import_rows` and validates it — nothing touches the domain tables. The
admin reviews the result, and only then does phase two commit.

`import_rows` is the reason the whole thing is safe:

  * it is the error report the admin fixes her sheet from (row_number points
    at the real line in her file, not at our internal ordering),
  * it records `entity_id` for everything created, which is what makes a
    whole-batch rollback possible,
  * it stores `source_hash` so the *next* sync of the same sheet can skip
    unchanged rows with a hash comparison instead of a field-by-field diff,
  * and it is the audit trail — who imported what, when, from which file.

`raw_payload` holds the row exactly as it appeared in the spreadsheet, which
means this table contains PHI. It needs a retention policy; see
the RETENTION note below.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ImportStatus(str, enum.Enum):
    """Lifecycle of one uploaded sheet."""
    PARSING = "parsing"        # file read, rows being written to import_rows
    MAPPING = "mapping"        # awaiting the admin's column-mapping approval
    PREVIEW = "preview"        # validated and resolved; awaiting final confirm
    # Accepted, but another import of the SAME entity is writing. Recorded
    # rather than refused: the admin's request should not be lost because
    # someone else got there a second earlier.
    QUEUED = "queued"
    COMMITTING = "committing"  # writing now, inside its time limit
    COMMITTED = "committed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ImportRowStatus(str, enum.Enum):
    """Per-row verdict. Set during validation, updated during commit."""
    PENDING = "pending"        # not yet validated
    CREATE = "create"          # validated; will insert
    UPDATE = "update"          # validated; matches an existing row, will patch
    SKIP = "skip"              # matches an existing row, nothing to write
    # Deliberately distinct from SKIP and from ERROR. A duplicate is not the
    # admin's mistake to go and fix (ERROR), and it is not a silent no-op
    # (SKIP) — it's a row that was consciously not imported because the exact
    # same record already exists, either earlier in this file or in the
    # database. It gets counted and reported so "150 rows in, 148 imported"
    # is always explainable.
    DUPLICATE = "duplicate"
    NEEDS_INPUT = "needs_input"  # blocked on an ambiguous FK the admin must resolve
    ERROR = "error"            # failed validation; will not be committed
    CREATED = "created"        # committed
    UPDATED = "updated"        # committed
    FAILED = "failed"          # commit attempted and raised


class ImportBatch(Base):
    """One uploaded spreadsheet, for one entity type.

    Deliberately scoped to a single entity (a "Leads sheet", a "Clients
    sheet") rather than a multi-tab workbook: the admin picks what she is
    importing before uploading, which removes any need to *infer* what the
    sheet is and leaves the mapper with the much narrower job of placing
    columns within a known schema.
    """

    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # "leads" | "clients" | ... — a key into the importer's entity registry
    # rather than a PG enum, so adding an importable entity needs no migration.
    entity: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    # Google Sheets URL when the admin pasted a link instead of uploading.
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)

    # Plain String rather than a PG enum: these lifecycles will gain states
    # (a "scheduled" sync, a "partially_committed") and a String costs no
    # migration to extend. The Python enums above are the source of truth for
    # valid values; store .value explicitly at every assignment.
    status: Mapped[str] = mapped_column(
        String, nullable=False, default=ImportStatus.PARSING.value
    )

    # {source_header: target_field | None}. Proposed by the matcher, then
    # corrected and approved by the admin — the approved version is what the
    # commit actually runs on, and what gets reused for the next upload of
    # the same sheet.
    column_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # The matcher's full per-column proposal: header, suggested field, why it
    # matched, sample values, parse rate, warning. Kept so any later GET of
    # this batch can render the mapping screen without re-deriving it.
    #
    # A LIST, not a dict keyed by header: Postgres reorders jsonb object keys,
    # which would shuffle the columns out of spreadsheet order. Arrays don't.
    columns: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # {field: {source_value: canonical_value}} for enum columns.
    value_mapping: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # {entity: {source_name: resolved_uuid}} — the admin's answers to
    # ambiguous foreign keys ("which Sarah Chen?"), keyed by the name string
    # so one decision resolves every row that used that name.
    fk_resolutions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # "DMY" | "MDY". Cannot be inferred safely from data alone (03/04/2024 is
    # valid under both), so it is an explicit choice with a detected default.
    date_order: Mapped[str] = mapped_column(String, nullable=False, default="MDY")

    # Sheet is authoritative for ALL fields including workflow state, not just
    # demographics. Correct while loading FBT's spreadsheet history into an
    # empty dashboard; wrong once the team is working in the app, because it
    # lets a stale sheet revert their status changes. Off by default, opt-in
    # per batch, and recorded here so it is always visible after the fact.
    migration_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Denormalized counts so the review screen and the batch list don't have
    # to aggregate import_rows on every render.
    create_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    update_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skip_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # How far a chunked commit has progressed. The API commits a bounded slice
    # per call and returns the next cursor — the backend is on Vercel, where a
    # long-running background task would be killed at the function timeout.
    commit_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Hash of everything a verdict depends on (mapping, value mapping, date
    # order, migration mode, resolutions). While it matches, the verdicts
    # stored on import_rows are still accurate and the preview is served from
    # them. Nulled on any invalidating change, so a missed invalidation costs
    # a slow preview rather than a wrong one.
    preview_marker: Mapped[str | None] = mapped_column(String, nullable=True)

    # When this run began writing. Distinct from updated_at, which any touch
    # bumps and so cannot answer "has this exceeded its time limit".
    run_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Position marker in the per-entity queue.
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Why the previous attempt stopped. Kept apart from `error` so a timeout
    # reads differently from a parse failure, and so the history can offer
    # Resume with a reason attached.
    last_failure: Mapped[str | None] = mapped_column(Text, nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Heartbeat for the single-active-import lock. Every commit chunk advances
    # commit_cursor, which bumps this — so a running import keeps proving it's
    # alive, and one abandoned mid-way goes quiet and stops blocking others.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    rows: Mapped[list["ImportRow"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class ImportRow(Base):
    """One spreadsheet row, its verdict, and what it produced.

    RETENTION: raw_payload contains PHI, and is redacted in place
    IMPORT_ROW_RETENTION_DAYS after the batch settles — see
    services/retention_service.py, run daily from scheduler_service.py. The
    row_number, status, errors and entity_id survive redaction, because those
    are what make an import explainable afterwards and what a rollback walks.
    """

    __tablename__ = "import_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="uq_import_rows_batch_row"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 1-based line number in the ADMIN'S file, header row included, so an
    # error message can say "row 340" and she can go straight to it.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # The row as it appeared in the sheet: {source_header: cell_value}.
    # NEVER edited — this is the audit trail of what the spreadsheet said.
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Corrections the admin typed on the review screen, keyed by FIELD name:
    # {"email": "user@example.com"}. Layered over raw_payload at
    # validation time so a one-character typo doesn't need a round trip
    # through the spreadsheet, while leaving the original readable.
    overrides: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The row after mapping, normalization and FK resolution — what the commit
    # will actually write. Null until validation runs.
    normalized_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Hash of the normalized values. Lets the next sync of this sheet skip
    # unchanged rows without diffing every field.
    source_hash: Mapped[str | None] = mapped_column(String, nullable=True, index=True)

    status: Mapped[str] = mapped_column(
        String, nullable=False, default=ImportRowStatus.PENDING.value, index=True
    )
    # Human-readable and field-scoped: [{"field": "email", "message": "..."}]
    errors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Field-level before/after for UPDATE rows, so the review screen can show
    # what will change rather than just "this row will be updated". Also
    # records fields the sheet tried to change but the policy refused, which
    # is what teaches the admin where each field actually lives.
    diff: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # The domain row this produced. Set on commit; the rollback reads it.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    batch: Mapped["ImportBatch"] = relationship(back_populates="rows")
