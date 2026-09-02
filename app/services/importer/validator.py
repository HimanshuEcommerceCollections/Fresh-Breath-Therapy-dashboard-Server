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

import enum as py_enum
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, or_, select
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
    # For DUPLICATE rows: "file" (an earlier row in this same sheet) or
    # "database" (a record that already exists). Kept apart so the summary can
    # say which — "fix your spreadsheet" and "already imported" are different
    # problems with different actions.
    duplicate_source: str | None = None


def _jsonable(value):
    """JSONB-safe rendering. Dates keep ISO form, money keeps precision."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, py_enum.Enum):
        # The VALUE, not the member. str(PaymentMethod.CASH) is
        # "PaymentMethod.CASH", while a sheet supplies "cash" — so a natural
        # key containing an enum could never match an existing row, and every
        # already-recorded payment looked brand new.
        return value.value
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _key_part(value) -> str:
    """One component of a natural key, rendered so equal values compare equal.

    Money is the reason this isn't just str(): a sheet's "400" parses to
    Decimal("400.00") while the same amount already stored can be in hand as
    Decimal("400"). Numerically identical, textually not — and comparing the
    text made every already-recorded payment look brand new.
    """
    if isinstance(value, Decimal):
        try:
            return str(value.quantize(Decimal("0.01")))
        except (ArithmeticError, ValueError):
            return str(value)
    if isinstance(value, float):
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    return str(_jsonable(value)).strip().lower()


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

    # Cross-field rule: a session's payment columns are optional as a GROUP,
    # but an amount with nobody covering it is not a payment. Caught here so
    # the admin sees it on the review screen, before anything is written —
    # _insert_session_payments raises on the same condition, but by then the
    # row has already cost a failed chunk.
    if entity.key == "sessions":
        has_amount = normalized.get("payment_amount") not in (None, "")
        has_method = bool(normalized.get("payment_method"))
        if has_amount and not has_method:
            errors.append({
                "field": "payment_method", "column": None,
                "message": (
                    "This session has a payment amount but no payment method. "
                    "Map a method column, or leave the amount unmapped to "
                    "import the session without a payment."
                ),
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
        parts.append(_key_part(value))
    return "|".join(parts)



def _index_bounds(
    entity: EntitySpec,
    rows: list[tuple[int, dict]],
    prepared: dict[int, tuple[dict, list[dict]]] | None,
    fk: FkResolution,
) -> dict[str, set[str]] | None:
    """Values to bound the existing-record read by, per natural-key column.

    Foreign-key components are taken from the RESOLUTION, not from the
    normalized row: at this stage the row still holds the name the sheet used,
    and only `fk.row_assignments` knows which record it points at. Text
    components come from the normalized values.

    Returns None when there is nothing usable, which reads the whole table —
    correct, just unbounded. Better a slow read than a wrong one.
    """
    if not prepared:
        return None

    ref_name = "external_id" if entity.key == "leads" else "external_ref"
    bounds: dict[str, set[str]] = {}

    for name in entity.natural_key:
        spec = entity.field(name)
        if spec is None:
            continue
        values: set[str] = set()
        if spec.kind is FieldKind.FK:
            for row_number, _ in rows:
                assigned = fk.row_assignments.get((name, row_number))
                if assigned and assigned != FkResolution.WILL_CREATE:
                    values.add(str(assigned))
        elif spec.kind in (FieldKind.TEXT, FieldKind.EMAIL):
            for normalized, _ in prepared.values():
                value = normalized.get(name)
                if value not in (None, ""):
                    values.add(_key_part(value))
        if values:
            bounds[name] = values

    refs = {
        str(normalized.get(ref_name))
        for normalized, _ in prepared.values()
        if normalized.get(ref_name)
    }
    if refs and entity.model.__table__.columns.get(ref_name) is not None:
        bounds[ref_name] = refs

    return bounds or None


async def load_existing_index(
    db: AsyncSession, entity: EntitySpec,
    bounds: dict[str, set[str]] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """(by_external_ref, by_natural_key) over every existing row.

    One query for the batch rather than one per row: a 5,000-row import
    through a Supabase pooler capped at 15 clients cannot afford per-row
    lookups.
    """
    query = select(entity.model)

    # Bound the read by the sheet where we safely can.
    #
    # The natural key is a COMPOSITE ("name|email"), and reconstructing that in
    # SQL differs per entity — so instead this filters on the individual key
    # COLUMNS with an OR, which is a superset of the rows that could match.
    # A superset is the point: the composite keys are still built in Python
    # exactly as before, so no verdict can change, but the rows crossing the
    # wire become proportional to the import rather than to the clinic.
    #
    # Only text-ish columns are filtered. Dates and money in a natural key
    # (payments) are left unconstrained rather than risk a type mismatch
    # silently excluding a row that should have matched — being a looser
    # superset costs bandwidth, being a subset would cost correctness.
    # Bound the read by the sheet where we safely can.
    #
    # `bounds` is {field_name: {already-comparable values}} built by the
    # caller, which is the only place that knows how to make an FK value
    # comparable: at this point a natural-key FK is still the NAME from the
    # sheet, and the name->id swap does not happen until the per-row loop
    # below. Resolving it here by calling uuid.UUID() on a name is exactly the
    # bug the full-output differential caught — it crashed every entity whose
    # natural key contains a foreign key (payments, sessions)
    # while leaving the text-keyed entities working, so nothing noticed.
    #
    # The natural key is a COMPOSITE, and reconstructing it in SQL differs per
    # entity, so these conditions are OR'd per column: a superset of the rows
    # that could match. A superset is the point — the composite keys are still
    # built in Python exactly as before, so no verdict can change, while the
    # rows crossing the wire shrink to the size of the import.
    if bounds:
        conditions = []
        for name, values in bounds.items():
            if not values:
                continue
            spec = entity.field(name)
            if spec is None:
                # The reference column, which is not a registry field.
                column = entity.model.__table__.columns.get(name)
                if column is not None:
                    conditions.append(column.in_(values))
                continue
            column = entity.model.__table__.columns.get(
                model_attr(name, spec.kind)
            )
            if column is None:
                continue
            if spec.kind is FieldKind.FK:
                # Already resolved ids, supplied by the caller.
                try:
                    conditions.append(column.in_([uuid.UUID(v) for v in values]))
                except (ValueError, AttributeError, TypeError):
                    # An unresolved value means this column cannot narrow the
                    # read. Skipping it widens the superset; it never shrinks it.
                    continue
            elif spec.kind in (FieldKind.TEXT, FieldKind.EMAIL):
                conditions.append(func.lower(column).in_(values))

        if conditions:
            query = query.where(or_(*conditions))

    result = await db.execute(query)
    rows_found = result.scalars().all()

    ref_column = "external_id" if entity.key == "leads" else "external_ref"
    by_ref: dict[str, object] = {}
    by_key: dict[str, object] = {}

    for row in rows_found:
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
    prepared: dict[int, tuple[dict, list[dict]]] | None = None,
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

    `prepared` is {row_number: (normalized, errors)} from an earlier
    normalize pass. The preview has to normalize every row anyway to give the
    foreign-key resolver something to group on, and without this it then threw
    that work away and normalized all of them a second time. Optional, because
    the single-row re-check path (PATCH /rows/{n}) has no earlier pass and
    still needs to be able to call this on its own.
    """
    entity = get_entity(entity_key)
    fk = fk or FkResolution(groups={}, row_assignments={})
    overrides_by_row = overrides_by_row or {}
    by_ref, by_key = await load_existing_index(
        db, entity, _index_bounds(entity, rows, prepared, fk)
    )
    ref_field = "external_id" if entity.key == "leads" else "external_ref"

    verdicts: list[RowVerdict] = []
    # Rows within one file can collide with each other, not just with the
    # database — the same client listed twice, the same payment pasted twice.
    # Two levels: an identical ROW is a duplicate to skip; the same record
    # described two different ways is a contradiction to report.
    seen_hashes: dict[str, int] = {}
    seen_keys: dict[str, int] = {}

    for row_number, raw_payload in rows:
        ready = prepared.get(row_number) if prepared else None
        if ready is not None:
            # Copied, not aliased: the FK swap below rewrites `normalized` in
            # place, and these dicts belong to the caller — the resolver is
            # still holding the same objects.
            normalized, errors = dict(ready[0]), list(ready[1])
        else:
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

        # ── the same row twice in the same file ───────────────────────────
        # Compared on the FULL row (source_hash), not just the natural key.
        # Two rows sharing a key but differing anywhere else are not the same
        # entry — they're the sheet contradicting itself about one record, and
        # collapsing them would silently pick whichever came first.
        if source_hash in seen_hashes:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.DUPLICATE.value,
                normalized=jsonable, source_hash=source_hash,
                duplicate_source="file",
                errors=[{
                    "field": None, "column": None,
                    "message": (
                        f"Identical to row {seen_hashes[source_hash]} in this "
                        "file — importing once."
                    ),
                }],
            ))
            continue
        seen_hashes[source_hash] = row_number

        if key and key in seen_keys:
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.ERROR.value,
                normalized=jsonable, source_hash=source_hash,
                errors=[{
                    "field": None, "column": None,
                    "message": (
                        f"Row {seen_keys[key]} describes this same record "
                        "differently. Decide which is right and remove the other."
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
            # Payments are append-only. An identical transaction already on
            # file is the same payment entered twice — the natural key covers
            # client, date, amount AND method, so a real second payment differs
            # somewhere and lands as a CREATE above.
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.DUPLICATE.value,
                normalized=jsonable, source_hash=source_hash,
                entity_id=str(existing.id), duplicate_source="database",
                errors=[{
                    "field": None, "column": None,
                    "message": (
                        "This exact payment is already recorded — same client, "
                        "date, amount and method."
                    ),
                }],
            ))
            continue

        changes, refused = _diff(
            entity, existing, normalized, migration_mode=migration_mode
        )

        if not changes and not refused:
            # Every field matches what's already stored: importing it would
            # write nothing at all.
            verdicts.append(RowVerdict(
                row_number=row_number, status=ImportRowStatus.DUPLICATE.value,
                normalized=jsonable, source_hash=source_hash,
                entity_id=str(existing.id), duplicate_source="database",
                errors=[{
                    "field": None, "column": None,
                    "message": "Already in the dashboard, with nothing different.",
                }],
            ))
            continue

        status = (
            ImportRowStatus.UPDATE.value if changes else ImportRowStatus.SKIP.value
        )
        verdicts.append(RowVerdict(
            row_number=row_number, status=status,
            normalized=jsonable, source_hash=source_hash,
            entity_id=str(existing.id),
            diff={"changes": changes, "refused": refused},
        ))

    return verdicts


def summarize(verdicts: list[RowVerdict]) -> dict[str, int]:
    """Per-status totals, plus the two duplicate sub-totals.

    Duplicates are split because the two have different meanings: one says the
    spreadsheet repeats itself, the other says this was imported already. Both
    are reported so the gap between "150 rows" and "142 imported" is never
    unexplained.
    """
    counts = {
        "create": 0, "update": 0, "skip": 0, "needs_input": 0, "error": 0,
        "duplicate": 0, "duplicate_in_file": 0, "duplicate_in_database": 0,
    }
    for verdict in verdicts:
        if verdict.status in counts:
            counts[verdict.status] += 1
        if verdict.status == ImportRowStatus.DUPLICATE.value:
            if verdict.duplicate_source == "file":
                counts["duplicate_in_file"] += 1
            elif verdict.duplicate_source == "database":
                counts["duplicate_in_database"] += 1
    return counts
