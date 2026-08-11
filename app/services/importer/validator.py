"""Deciding what each row will do, without doing any of it.

Produces one verdict per spreadsheet row — create, update, skip, needs-input
or error — plus, for updates, a field-level before/after diff. This is the
entire content of the review screen, and it runs against the real database
(matching existing records, resolving foreign keys) while writing nothing.

Two behaviours here are load-bearing:

`_diff` enforces the Writability policy from registry.py. On an existing row
the sheet may change demographics but not workflow state, because the team is
working in the dashboard and a stale sheet must not revert them. Crucially,
refused changes are *recorded* in the diff rather than dropped — the review
screen shows "sheet says completed_program — ignored (dashboard-owned)", so
the admin learns where the field lives instead of silently editing her sheet
for months and wondering why nothing happens.

`source_hash` is what makes the second sync cheap: an unchanged row is
recognised by its hash and skipped without comparing a single field.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_batch import ImportRowStatus
from app.services.importer import normalizers
from app.services.importer.registry import (
    EntitySpec, FieldKind, Writability, get_entity,
)
from app.services.importer.resolver import FkResolution

logger = logging.getLogger(__name__)


def model_attr(field_name: str, kind: FieldKind) -> str:
    """Registry field name -> SQLAlchemy attribute.

    Foreign keys are named for the thing ("therapist") but stored as the id
    ("therapist_id"); everything else matches one-to-one.
    """
    return f"{field_name}_id" if kind is FieldKind.FK else field_name


@dataclass
class RowVerdict:
    row_number: int
    status: str
    normalized: dict | None = None
    errors: list[dict] = dc_field(default_factory=list)
    diff: dict | None = None
    source_hash: str | None = None
    # The existing record this row matched, if any.
    entity_id: str | None = None


def _jsonable(value):
    """JSONB-safe rendering. Dates keep ISO form, money keeps precision."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _hash(normalized: dict) -> str:
    payload = {k: _jsonable(v) for k, v in sorted(normalized.items())}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _same(a, b) -> bool:
    """Compare a normalized cell against a value already in the database.

    Types cross layers here — Numeric comes back as Decimal, a Date column as
    date, and the sheet may have produced either — so compare on meaning
    rather than on `==` between mismatched types.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    # Resolved foreign keys arrive as UUID *strings* from the JSONB payload
    # while the database hands back UUID *objects*. Without this, every FK
    # column reports itself as changed on every update.
    if isinstance(a, uuid.UUID) or isinstance(b, uuid.UUID):
        return str(a).lower() == str(b).lower()
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except Exception:
            return str(a) == str(b)
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a.replace(microsecond=0) == b.replace(microsecond=0)
    if isinstance(a, (date, time)) or isinstance(b, (date, time)):
        return str(a) == str(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


# ── mapping + normalizing one row ─────────────────────────────────────────

def normalize_row(
    entity: EntitySpec,
    raw_payload: dict,
    mapping: dict[str, str | None],
    *,
    date_order: str,
    value_mapping: dict[str, dict[str, str]] | None = None,
    overrides: dict[str, object] | None = None,
) -> tuple[dict, list[dict]]:
    """{header: cell} -> ({field: value}, errors).

    Every cell is attempted even after one fails, so the admin gets the whole
    list of problems in a row at once rather than fixing them one upload at a
    time.

    `overrides` are corrections the admin typed on the review screen, keyed by
    FIELD name. They replace the sheet's cell before parsing — so a mistyped
    email can be fixed in place instead of forcing a round trip through the
    spreadsheet for one character. raw_payload is never mutated: it stays the
    record of what the sheet actually said, which is what the audit trail is
    for.
    """
    value_mapping = value_mapping or {}
    overrides = overrides or {}
    normalized: dict = {}
    errors: list[dict] = []

    for header, field_name in mapping.items():
        if not field_name:
            continue
        spec = entity.field(field_name)
        if spec is None or spec.writable is Writability.NEVER:
            # NEVER fields are derived; a sheet column pointed at one is
            # ignored rather than treated as an error.
            continue
        cell = (
            overrides[field_name]
            if field_name in overrides
            else raw_payload.get(header)
        )
        try:
            normalized[field_name] = normalizers.normalize_cell(
                spec, cell,
                date_order=date_order,
                value_map=value_mapping.get(field_name),
            )
        except normalizers.CellError as exc:
            errors.append({
                "field": field_name, "column": header, "message": str(exc),
                # The cell that failed. It never reaches `normalized` (that's
                # what failing means), so without carrying it here the review
                # screen has nothing to seed its correction box with and the
                # admin retypes a whole value to fix one character.
                "value": None if cell is None else str(cell),
            })

    # A required field whose column exists but is blank is caught above; this
    # catches one that was never mapped at all, which the UI should have
    # blocked — re-checked here because the button is not the guard.
    for spec in entity.required_fields:
        if spec.name not in normalized and not any(
            e["field"] == spec.name for e in errors
        ):
            errors.append({
                "field": spec.name, "column": None,
                "message": f"{spec.label} is required but no column is mapped to it",
            })

    return normalized, errors


# ── matching against what already exists ──────────────────────────────────

def natural_key(entity: EntitySpec, values: dict) -> str | None:
    """Identity for a row that has no external_ref yet — i.e. first import."""
    if not entity.natural_key:
        return None
    parts = []
    for name in entity.natural_key:
        spec = entity.field(name)
        value = values.get(name)
        if value is None or value == "":
            return None  # incomplete key identifies nothing
        parts.append(str(_jsonable(value)).strip().lower())
    return "|".join(parts)


async def load_existing_index(
    db: AsyncSession, entity: EntitySpec
) -> tuple[dict[str, object], dict[str, object]]:
    """(by_external_ref, by_natural_key) over every existing row.

    One query for the batch rather than one per row: a 5,000-row import
    through a Supabase pooler capped at 15 clients cannot afford per-row
    lookups.
    """
    result = await db.execute(select(entity.model))
    rows = result.scalars().all()

    ref_column = "external_id" if entity.key == "leads" else "external_ref"
    by_ref: dict[str, object] = {}
    by_key: dict[str, object] = {}

    for row in rows:
        ref = getattr(row, ref_column, None)
        if ref:
            by_ref[str(ref)] = row

        values = {}
        for name in entity.natural_key:
            spec = entity.field(name)
            if spec is None:
                continue
            values[name] = getattr(row, model_attr(name, spec.kind), None)
        key = natural_key(entity, values)
        # First writer wins: if two existing rows collapse to the same natural
        # key the data is already ambiguous, and quietly updating the second
        # one would be a guess.
        if key and key not in by_key:
            by_key[key] = row

    return by_ref, by_key


# ── diffing an update ─────────────────────────────────────────────────────

def _diff(
    entity: EntitySpec, existing, normalized: dict, *, migration_mode: bool
) -> tuple[dict, dict]:
    """-> (changes to apply, changes refused by policy).

    Refused changes are returned, not discarded, so the UI can explain them.
    """
    changes: dict = {}
    refused: dict = {}

    for field_name, new_value in normalized.items():
        spec = entity.field(field_name)
        if spec is None or spec.writable is Writability.NEVER:
            continue

        attr = model_attr(field_name, spec.kind)
        current = getattr(existing, attr, None)
        if _same(new_value, current):
            continue

        if spec.writable is Writability.INSERT_ONLY and not migration_mode:
            refused[field_name] = {
                "current": _jsonable(current),
                "sheet": _jsonable(new_value),
                "reason": "Managed in the dashboard — the sheet can't change it.",
            }
            continue

        changes[field_name] = {
            "from": _jsonable(current), "to": _jsonable(new_value),
        }

    return changes, refused


# ── the pass ──────────────────────────────────────────────────────────────

async def validate_rows(
    db: AsyncSession,
    entity_key: str,
    rows: list[tuple[int, dict]],
    *,
    mapping: dict[str, str | None],
    date_order: str = "MDY",
    value_mapping: dict[str, dict[str, str]] | None = None,
    fk: FkResolution | None = None,
    migration_mode: bool = False,
    overrides_by_row: dict[int, dict] | None = None,
) -> list[RowVerdict]:
    """Validate a whole batch. Reads the database; writes nothing.

    `fk` comes from resolver.resolve_foreign_keys with the admin's answers
    already folded in. Its PER-ROW assignments are what's read here, not its
    groups: two clients naming the same therapist can legitimately resolve to
    different people once the row's location is taken into account, so the
    row is the only safe unit to read.

    A row whose name is still ambiguous or missing becomes NEEDS_INPUT rather
    than ERROR — it isn't the row that's wrong, it's a question nobody has
    answered yet.
    """
    entity = get_entity(entity_key)
    fk = fk or FkResolution(groups={}, row_assignments={})
    overrides_by_row = overrides_by_row or {}
    by_ref, by_key = await load_existing_index(db, entity)
    ref_field = "external_id" if entity.key == "leads" else "external_ref"

    verdicts: list[RowVerdict] = []
    # Rows within one file can collide with each other, not just with the
    # database — the same client listed twice, the same payment pasted twice.
    seen_keys: dict[str, int] = {}

    for row_number, raw_payload in rows:
        normalized, errors = normalize_row(
            entity, raw_payload, mapping,
            date_order=date_order, value_mapping=value_mapping,
            overrides=overrides_by_row.get(row_number),
        )

        if errors:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.ERROR.value,
                normalized={k: _jsonable(v) for k, v in normalized.items()},
                errors=errors,
            ))
            continue

        # Swap resolved FK names for ids, per row. A name still needing a
        # decision parks the row rather than failing it.
        blocked: list[dict] = []
        for spec in entity.fields:
            if spec.kind is not FieldKind.FK or spec.name not in normalized:
                continue
            source_value = normalized[spec.name]
            if source_value is None:
                continue

            lookup = (spec.name, row_number)
            if lookup not in fk.row_assignments:
                continue
            assigned = fk.row_assignments[lookup]

            if assigned == FkResolution.WILL_CREATE:
                # Keep the NAME. Creating the location is a write, and this
                # pass must not write — commit.py turns it into an id.
                pass
            elif assigned:
                normalized[spec.name] = assigned
            else:
                group = fk.groups.get(fk.row_groups.get(lookup, ""))
                blocked.append({
                    "field": spec.name, "column": None,
                    "message": (
                        group.message if group and group.message
                        else f'Unresolved {spec.label}: "{source_value}"'
                    ),
                })

        if blocked:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.NEEDS_INPUT.value,
                normalized={k: _jsonable(v) for k, v in normalized.items()},
                errors=blocked,
            ))
            continue

        source_hash = _hash(normalized)
        jsonable = {k: _jsonable(v) for k, v in normalized.items()}

        # Identity: the written-back reference first, natural key as fallback.
        existing = None
        ref = normalized.get(ref_field)
        if ref:
            existing = by_ref.get(str(ref))
        key = natural_key(entity, normalized)
        if existing is None and key:
            existing = by_key.get(key)

        if key and key in seen_keys and existing is None:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.ERROR.value,
                normalized=jsonable, source_hash=source_hash,
                errors=[{
                    "field": None, "column": None,
                    "message": (
                        f"Duplicates row {seen_keys[key]} in this same file. "
                        "Remove one of them."
                    ),
                }],
            ))
            continue
        if key:
            seen_keys.setdefault(key, row_number)

        if existing is None:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.CREATE.value,
                normalized=jsonable, source_hash=source_hash,
            ))
            continue

        if not entity.supports_update:
            # Payments: the ledger is append-only, so a transaction we already
            # hold is left exactly as it is.
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.SKIP.value,
                normalized=jsonable, source_hash=source_hash,
                entity_id=str(existing.id),
                diff={"note": "Already recorded — payments are never re-written."},
            ))
            continue

        changes, refused = _diff(
            entity, existing, normalized, migration_mode=migration_mode
        )
        status = (
            ImportRowStatus.UPDATE.value if changes else ImportRowStatus.SKIP.value
        )
        verdicts.append(RowVerdict(
            row_number=row_number, status=status,
            normalized=jsonable, source_hash=source_hash,
            entity_id=str(existing.id),
            diff={"changes": changes, "refused": refused} if (changes or refused) else None,
        ))

    return verdicts


def summarize(verdicts: list[RowVerdict]) -> dict[str, int]:
    counts = {
        "create": 0, "update": 0, "skip": 0, "needs_input": 0, "error": 0,
    }
    for verdict in verdicts:
        if verdict.status in counts:
            counts[verdict.status] += 1
    return counts
