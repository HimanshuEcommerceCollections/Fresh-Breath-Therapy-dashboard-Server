"""Request/response shapes for the spreadsheet import."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.base import ORMBase


class EntityFieldInfo(BaseModel):
    """One target field, as the mapping dropdown needs to render it."""
    name: str
    label: str
    kind: str
    required: bool
    # "always" | "insert_only" | "never" — drives the badge explaining why a
    # field can't be changed on an existing record.
    writable: str
    help_text: str | None = None
    # Populated for enum fields so the value-mapping panel can render inline
    # beneath the column, rather than as a separate step.
    options: list[str] | None = None


class EntityInfo(BaseModel):
    key: str
    label: str
    fields: list[EntityFieldInfo]
    depends_on: list[str]
    notes: list[str] = []
    # Rows already in the database. Also what decides `ready`.
    existing_count: int = 0
    # False while a prerequisite entity is still empty — importing clients
    # before any therapist exists would fail every row on a NOT NULL FK, so
    # the picker greys it out instead.
    ready: bool = True
    blocked_by: list[str] = []


class ColumnSuggestionOut(BaseModel):
    header: str
    field: str | None = None
    confidence: float = 0.0
    reason: str = "none"
    samples: list[str] = []
    distinct_count: int = 0
    parse_rate: float | None = None
    warning: str | None = None


class ValueOption(BaseModel):
    """One distinct value in an enum column, and where it's headed."""
    source_value: str
    count: int
    mapped_to: str | None = None


class ValueMappingOut(BaseModel):
    field: str
    label: str
    options: list[str]
    values: list[ValueOption]

    @property
    def unmapped(self) -> int:
        return sum(1 for v in self.values if not v.mapped_to)


class FkCandidateOut(BaseModel):
    id: str
    label: str


class FkRowRefOut(BaseModel):
    """One import row inside a group, for the per-row override list."""
    row_number: int
    label: str


class FkGroupOut(BaseModel):
    # Stable id the admin's answer is stored against. Includes the
    # disambiguator, so "Sarah Chen at Greensboro" and "Sarah Chen at
    # Downtown" are two separately answerable questions.
    key: str
    field: str
    target: str
    source_value: str
    row_count: int
    # "resolved" | "ambiguous" | "missing" | "will_create"
    status: str
    # The context that split this group out of a larger one — the location
    # these particular rows belong to. None when the name wasn't ambiguous.
    disambiguator: str | None = None
    disambiguator_label: str | None = None
    resolved_id: str | None = None
    # Set when the disambiguator resolved it rather than the name alone
    # ("location", "set per row"). Shown so an inference is never silent.
    matched_by: str | None = None
    candidates: list[FkCandidateOut] = []
    suggestion: FkCandidateOut | None = None
    rows: list[FkRowRefOut] = []
    message: str | None = None


class ImportBatchSummary(ORMBase):
    id: uuid.UUID
    entity: str
    filename: str
    status: str
    total_rows: int
    create_count: int
    update_count: int
    skip_count: int
    error_count: int
    migration_mode: bool
    date_order: str
    created_at: datetime
    committed_at: datetime | None = None
    error: str | None = None


class ImportBatchDetail(ImportBatchSummary):
    column_mapping: dict[str, str | None] = {}
    columns: list[ColumnSuggestionOut] = []
    value_mappings: list[ValueMappingOut] = []
    # Required fields with no column pointed at them. Non-empty means the
    # commit is blocked — every one of these is NOT NULL in Postgres.
    unmapped_required: list[str] = []
    date_order_confident: bool = True
    headers: list[str] = []


class MappingUpdate(BaseModel):
    """The admin's corrections to the proposed mapping."""
    column_mapping: dict[str, str | None] | None = None
    # {field: {source_value: canonical_value}}
    value_mapping: dict[str, dict[str, str]] | None = None
    date_order: str | None = Field(default=None, pattern="^(DMY|MDY)$")
    migration_mode: bool | None = None


class RowEdit(BaseModel):
    """Corrections typed on the review screen, keyed by field name.

    A field set to null or blank drops its correction and falls back to the
    spreadsheet's own cell, so a fix can always be undone.
    """
    values: dict[str, Any | None]


class CreateMissingRecord(BaseModel):
    """Create a referenced record that the sheet names but the database
    doesn't have yet, and resolve the question it was blocking.

    The alternative was to offer the admin a dropdown of every OTHER record,
    which for a name that simply isn't there is the wrong action entirely —
    picking an unrelated client for "Isabella Grant" files a session against
    the wrong patient.
    """
    # Which question this answers. The created record is resolved against it
    # in the same transaction, so the admin isn't left to re-pick it.
    group_key: str
    # Registry key of what to create: "clients", "therapists", "packages".
    target: str
    # Field name -> value, using the target entity's own field names. FK
    # fields carry the chosen record's UUID.
    values: dict[str, Any]


class CreatedRecord(BaseModel):
    candidate: FkCandidateOut
    batch: "ImportBatchSummary"


class FkResolutionUpdate(BaseModel):
    """Answers to the "which Sarah Chen?" questions.

    Two levels, because one answer per name is not always correct. `groups`
    is keyed by group key — which already accounts for the disambiguator, so
    the Greensboro rows and the Downtown rows are answered separately. `rows`
    overrides individual rows within a group and always wins, for the case
    where even the location doesn't separate two same-named therapists.
    """
    # {group_key: target_uuid}
    groups: dict[str, str] = {}
    # {field: {row_number: target_uuid}}
    rows: dict[str, dict[str, str]] = {}


class RowPreview(BaseModel):
    row_number: int
    status: str
    # [{field, column, message, value}] — `value` is the cell that failed, so
    # the review screen can seed its inline correction box with it.
    errors: list[dict] = []
    diff: dict | None = None
    values: dict = {}


class ImportPreview(BaseModel):
    batch: ImportBatchSummary
    counts: dict[str, int]
    # Groups still needing an answer. While non-empty, commit is refused.
    fk_groups: list[FkGroupOut] = []
    blocking_fk_count: int = 0
    unmapped_required: list[str] = []
    # Enum columns and their value mappings, so the review screen can send
    # the admin straight back to an unmapped value rather than leaving her
    # with a row error she can't act on.
    value_mappings: list[ValueMappingOut] = []
    rows: list[RowPreview] = []
    total_rows: int = 0
    can_commit: bool = False
    blockers: list[str] = []


class CommitResult(BaseModel):
    # Set when another import of the SAME entity is writing. The request was
    # accepted and recorded, not refused — the client keeps polling and this
    # batch starts by itself when the entity frees up.
    queued: bool = False
    queue_position: int = 0
    queued_behind: str | None = None
    processed: int
    created: int
    updated: int
    failed: int
    remaining: int
    done: bool
    batch: ImportBatchSummary


class RollbackResult(BaseModel):
    deleted: int
    reverted: int
    batch: ImportBatchSummary
