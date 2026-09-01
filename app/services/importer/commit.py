"""Writing an approved batch to the database, and undoing it.

Chunked on purpose. This backend runs on Vercel, where a function is killed
at its timeout and a long-running BackgroundTask dies with it — so the commit
does a bounded slice per call and reports how much is left, and the client
polls. That also keeps each transaction small in front of a Supabase pooler
capped at 15 clients, and gives the admin a real progress bar for free.

The slice is defined by status, not by an offset: committed rows change
status and simply drop out of the "pending" filter. A call that dies halfway
therefore resumes exactly where it stopped, and re-calling never double-writes.

Three rules this module exists to enforce:

  * Payments are replayed in DATE order, not sheet order, through the same
    arithmetic as routers/payments.py — so total_paid, amount_due and each
    payment's balance_after come out identical to what the dashboard would
    have recorded had the payments been entered live, one at a time.
  * No notifications. The lead webhook raises one per lead, which is right
    for a trickle of live enquiries and catastrophic for a 500-row import.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Integer, Numeric, Time, bindparam, delete, func,
    select, text, update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import PaymentStatus, PtoTransactionType, SessionStatus
from app.models.pto_transaction import PtoTransaction
from app.services.pto_service import ACCRUAL_RATE
from app.models.import_batch import ImportBatch, ImportRow, ImportRowStatus, ImportStatus
from app.models.location import Location
from app.models.payment import Payment
from app.services.ids import uuid7
from app.services.importer.registry import FieldKind, Writability, get_entity
from app.services.importer.resolver import ensure_location
from app.services.importer.validator import model_attr

logger = logging.getLogger(__name__)

# Rows per call. Small enough to stay well inside a serverless timeout even
# large enough that a few thousand
# rows is a handful of calls rather than hundreds.
CHUNK_SIZE = 200

# How long a claimed-but-silent import keeps blocking others. Comfortably
# longer than any single chunk (each is bounded to stay inside a serverless
# timeout), short enough that an abandoned import isn't an all-day outage.
STALE_CLAIM_AFTER = timedelta(minutes=5)

# Arbitrary but fixed: every import commit contends for this one advisory
# lock, so two can never write concurrently even if both somehow passed the
# `committing` status check.
_ADVISORY_LOCK_KEY = 0x1_50_1_1_0_0_7  # "IMPORT" in a shape that fits an int32


def _entity_lock_key(entity: str) -> int:
    """Stable per-entity classid for pg_try_advisory_xact_lock(int, int).

    Index into _LOCK_SLOTS rather than a hash, so the value is small,
    readable in pg_locks, and cannot collide.
    """
    from app.services.importer.registry import _LOCK_SLOTS

    try:
        return _LOCK_SLOTS.index(entity)
    except ValueError:
        return len(_LOCK_SLOTS)


def _tier_sizes() -> list[int]:
    """Group sizes to try, largest first; bisect below the last.

    `[1]` reproduces the original per-row behaviour exactly, which is what the
    differential test uses to generate its reference output.
    """
    return list(settings.IMPORT_CHUNK_TIER_SIZES or [CHUNK_SIZE, 25])

PENDING = (ImportRowStatus.CREATE.value, ImportRowStatus.UPDATE.value)


@dataclass
class CommitProgress:
    processed: int
    created: int
    updated: int
    failed: int
    remaining: int
    done: bool


async def reap_expired_runs(db: AsyncSession, entity: str | None = None) -> list[str]:
    """Fail any run that has exceeded its time limit, and release its claim.

    A run is judged on `run_started_at`, not `updated_at`: a chunk loop that
    keeps making progress still bumps the heartbeat, so a heartbeat can only
    tell you a run is alive — never that it has taken too long.

    An expired run goes back to PREVIEW rather than to a dead FAILED state,
    with the reason on `last_failure`. That is what makes Resume appear: the
    rows it already wrote keep their CREATED/UPDATED status, so resuming picks
    up exactly where it stopped instead of writing anything twice.

    Deliberately NOT undoing the rows already written. "Rolled back as failed"
    could mean that, but undoing good writes and redoing them is strictly
    worse here — it doubles the work, and for payments it would unwind a
    partially-applied balance chain that the remaining rows are computed
    against. Status-based resumption already gives exactly-once writing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.IMPORT_MAX_RUNTIME_SECONDS
    )
    query = select(ImportBatch).where(
        ImportBatch.status == ImportStatus.COMMITTING.value,
        ImportBatch.run_started_at.isnot(None),
        ImportBatch.run_started_at < cutoff,
    )
    if entity is not None:
        query = query.where(ImportBatch.entity == entity)

    reaped = []
    for batch in (await db.execute(query)).scalars().all():
        minutes = settings.IMPORT_MAX_RUNTIME_SECONDS // 60
        batch.status = ImportStatus.PREVIEW.value
        batch.last_failure = (
            f"Timed out after {minutes} minute(s). "
            f"{batch.commit_cursor} row(s) were written and are kept — "
            "resuming continues from there."
        )
        batch.run_started_at = None
        reaped.append(str(batch.id))
    if reaped:
        await db.commit()
    return reaped


async def find_running_import(
    db: AsyncSession, entity: str, *, other_than: uuid.UUID | None = None
) -> ImportBatch | None:
    """The batch currently writing to THIS entity, if any.

    Serialised per entity rather than globally. Two imports into different
    tables genuinely do not contend: a therapists import and a sessions import
    touch different rows, and making one wait for the other bought nothing.

    Same-entity is where it matters: two imports into one table can both
    resolve the same natural key and each believe it is creating the row.

    Residual risk, accepted and worth naming: entities are not perfectly
    independent. A clients import can create the client a concurrent sessions
    import is trying to resolve. That does not corrupt data — an unresolvable
    name was already NEEDS_INPUT before either started. What can happen is a
    verdict going stale mid-run, which surfaces as a failed or parked row
    rather than a wrong one.
    """
    query = select(ImportBatch).where(
        ImportBatch.entity == entity,
        ImportBatch.status == ImportStatus.COMMITTING.value,
        # Self-releasing. An admin who closes the tab mid-import would
        # otherwise leave the entity claimed forever, with no way to clear it.
        # Each chunk bumps updated_at, so a live run renews its claim and an
        # abandoned one lapses.
        ImportBatch.updated_at > datetime.now(timezone.utc) - STALE_CLAIM_AFTER,
    )
    if other_than is not None:
        query = query.where(ImportBatch.id != other_than)
    return (await db.execute(query.limit(1))).scalar_one_or_none()


async def queue_position(db: AsyncSession, batch: ImportBatch) -> int:
    """How many queued batches for this entity are ahead of this one."""
    return int(await db.scalar(
        select(func.count()).select_from(ImportBatch).where(
            ImportBatch.entity == batch.entity,
            ImportBatch.status == ImportStatus.QUEUED.value,
            ImportBatch.queued_at < (batch.queued_at or datetime.now(timezone.utc)),
        )
    ) or 0)


async def promote_next_queued(db: AsyncSession, entity: str) -> ImportBatch | None:
    """Hand the entity to whoever has waited longest.

    Called when a run finishes, so the queue drains without a worker process:
    the promoted batch returns to PREVIEW, and its own client — which is
    polling — picks it up on the next tick. That keeps this serverless-safe
    with no scheduler and no external queue.
    """
    nxt = (await db.execute(
        select(ImportBatch)
        .where(
            ImportBatch.entity == entity,
            ImportBatch.status == ImportStatus.QUEUED.value,
        )
        .order_by(ImportBatch.queued_at)
        .limit(1)
    )).scalar_one_or_none()
    if nxt is None:
        return None
    nxt.status = ImportStatus.PREVIEW.value
    nxt.queued_at = None
    await db.commit()
    return nxt


def external_ref_for(batch_id: uuid.UUID, row_number: int) -> str:
    """Deterministic, so re-running a batch reproduces the same references
    rather than minting new ones and duplicating every row."""
    return f"import:{batch_id}:{row_number}"


def _pending_query(batch: ImportBatch):
    query = select(ImportRow).where(
        ImportRow.batch_id == batch.id, ImportRow.status.in_(PENDING)
    )
    # Sheet order, for every entity. Payments used to be forced into date
    # order because each row's balance_after depended on the ones before it.
    # A payment no longer carries a running balance, so the order in which two
    # payments are inserted has no effect on what either of them says.
    return query.order_by(ImportRow.row_number)


async def _resolve_auto_create_fks(db: AsyncSession, entity, values: dict) -> None:
    """Turn any still-unresolved auto-create foreign key into a real id.

    Only locations qualify. The validator leaves these as the NAME string
    rather than a UUID, because resolving one means creating a clinic row and
    validation is required to write nothing. So by the time we get here the
    value is either already an id (it matched an existing clinic) or a name
    that needs one.
    """
    for spec in entity.fields:
        if spec.kind is not FieldKind.FK or not spec.fk_auto_create:
            continue
        value = values.get(spec.name)
        if value in (None, ""):
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            values[spec.name] = str(await ensure_location(db, str(value)))


def _coerce(model, name: str, value):
    """Restore the Python type a value had before it went through JSONB.

    normalize_cell produces real `date`, `time` and `Decimal` objects, but
    they are stored on import_rows.normalized_payload as JSONB — which only
    has strings and numbers. Reading them straight back and handing them to
    asyncpg fails with "expected a datetime.date, got 'str'", so the column's
    own type is what decides how to rebuild them.

    A bare date going into a timestamptz is rejected too, hence the explicit
    widening to midnight UTC rather than letting the driver guess.
    """
    if value is None:
        return None
    column = model.__table__.columns.get(name)
    if column is None:
        return value
    ctype = column.type

    if isinstance(ctype, DateTime):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, time.min)
        else:
            text_value = str(value)
            parsed = (
                datetime.fromisoformat(text_value)
                if ("T" in text_value or " " in text_value)
                else datetime.combine(date.fromisoformat(text_value), time.min)
            )
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    if isinstance(ctype, Date):
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    if isinstance(ctype, Time):
        return value if isinstance(value, time) else time.fromisoformat(str(value))

    if isinstance(ctype, Numeric):
        return value if isinstance(value, Decimal) else Decimal(str(value))

    if isinstance(ctype, Integer):
        return int(value)

    if isinstance(ctype, Boolean):
        return bool(value)

    return value


# Fields that live on a DIFFERENT table from the entity being imported.
# Sessions carry their payment's columns (see registry.SESSIONS), because a
# payment cannot exist without a session and so has no sheet of its own.
# _model_kwargs must not hand these to the Session constructor.
SATELLITE_FIELDS: dict[str, frozenset[str]] = {
    "sessions": frozenset({"payment_amount", "payment_method", "payment_status"}),
}


def _model_kwargs(entity, values: dict) -> dict:
    """Normalized field values -> model constructor kwargs."""
    satellites = SATELLITE_FIELDS.get(entity.key, frozenset())
    kwargs = {}
    for spec in entity.fields:
        if spec.name in satellites:
            continue
        if spec.name not in values or spec.writable is Writability.NEVER:
            continue
        value = values[spec.name]
        if spec.kind is FieldKind.FK:
            kwargs[model_attr(spec.name, spec.kind)] = (
                uuid.UUID(value) if value else None
            )
        else:
            kwargs[spec.name] = _coerce(entity.model, spec.name, value)
    return kwargs


# Postgres speaks in constraint names; the admin needs a sentence and an
# action. Anything unrecognised falls back to the FIRST line only — a full
# SQLAlchemy dump with the INSERT statement and every bound parameter is
# unreadable, and it leaks ids into the UI.
def humanize(exc: Exception) -> str:
    # `detail`, not `text` — this module imports sqlalchemy's text() at the
    # top and a local of that name shadows it inside this function.
    detail = str(exc)

    if "duplicate key" in detail or "UniqueViolationError" in detail:
        match = re.search(r"Key \((?P<cols>[^)]+)\)=", detail)
        if match:
            columns = match.group("cols").replace("_id", "").replace("_", " ")
            return f"Another record already has the same {columns}."
        return "A record with these details already exists."
    if "ForeignKeyViolationError" in detail:
        return "This row points at a record that no longer exists."
    if "NotNullViolationError" in detail:
        match = re.search(r'column "([^"]+)"', detail)
        return (
            f"Required value missing: {match.group(1)}."
            if match else "A required value is missing."
        )
    if "invalid input for query argument" in detail:
        return "A value couldn't be stored in the format this field expects."
    if "StringDataRightTruncationError" in detail or "value too long" in detail:
        return "A value is too long for its field."

    return detail.strip().splitlines()[0][:300]


@dataclass
class _Ctx:
    """Everything the group appliers need, resolved once per chunk."""
    entity: object
    ref_field: str
    has_ref: bool
    created_by: uuid.UUID | None
    # name(lowercased) -> id, for auto-created locations. Filled per group so
    # 200 rows naming the same six clinics cost one SELECT and one INSERT
    # rather than 400 round trips.
    locations: dict[str, uuid.UUID] = field(default_factory=dict)


@dataclass
class _Outcome:
    entity_id: uuid.UUID | None = None
    error: str | None = None


async def _prepare_locations(db: AsyncSession, ctx: _Ctx, rows) -> None:
    """Resolve every auto-create FK name in this group in two statements.

    This is where the gap between the documented 3 round trips per row and the
    measured 5.5 came from: each therapist row separately looked up, and
    sometimes created, its clinic.
    """
    specs = [
        s for s in ctx.entity.fields
        if s.kind is FieldKind.FK and s.fk_auto_create
    ]
    if not specs:
        return

    wanted: set[str] = set()
    for row in rows:
        values = row.normalized_payload or {}
        for spec in specs:
            value = values.get(spec.name)
            if not value:
                continue
            try:
                uuid.UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                wanted.add(str(value).strip())

    wanted = {w for w in wanted if w and w.lower() not in ctx.locations}
    if not wanted:
        return

    found = await db.execute(
        select(Location.id, Location.name).where(
            func.lower(Location.name).in_([w.lower() for w in wanted])
        )
    )
    for location_id, name in found.all():
        ctx.locations[name.strip().lower()] = location_id

    missing = [w for w in wanted if w.lower() not in ctx.locations]
    if missing:
        new_rows = [{"id": uuid7(), "name": name} for name in missing]
        await db.execute(insert(Location), new_rows)
        for entry in new_rows:
            ctx.locations[entry["name"].strip().lower()] = entry["id"]


def _apply_location_map(ctx: _Ctx, values: dict) -> None:
    for spec in ctx.entity.fields:
        if spec.kind is not FieldKind.FK or not spec.fk_auto_create:
            continue
        value = values.get(spec.name)
        if not value:
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            resolved = ctx.locations.get(str(value).strip().lower())
            if resolved is not None:
                values[spec.name] = str(resolved)


async def _apply_group(
    db: AsyncSession, ctx: _Ctx, batch: ImportBatch, rows: list[ImportRow]
) -> dict[uuid.UUID, _Outcome]:
    """Write a whole group with one statement per table, per operation.

    Raises on the first problem — the caller's tiering decides how finely to
    retry. Nothing here inspects individual rows for validity; the preview
    already did that, so the clean path is expected to dominate.
    """
    outcomes: dict[uuid.UUID, _Outcome] = {}
    await _prepare_locations(db, ctx, rows)

    creates = [r for r in rows if r.status == ImportRowStatus.CREATE.value]
    updates = [r for r in rows if r.status == ImportRowStatus.UPDATE.value]

    if creates:
        outcomes.update(await _insert_records(db, ctx, batch, creates))

    for row in updates:
        outcomes[row.id] = _Outcome(entity_id=row.entity_id)
    if updates:
        await _apply_updates(db, ctx, batch, updates)

    return outcomes


def _bind_chunks(rows: list[dict], columns: int) -> list[list[dict]]:
    """Split so no statement exceeds Postgres' 65,535 bind parameters."""
    per = max(1, 60000 // max(columns, 1))
    return [rows[i:i + per] for i in range(0, len(rows), per)]


async def _insert_records(
    db: AsyncSession, ctx: _Ctx, batch: ImportBatch, rows: list[ImportRow]
) -> dict[uuid.UUID, _Outcome]:
    """One batched INSERT per table.

    Payments used to need their own path, replaying each enrollment's running
    balance in date order under a row lock. Flat payments need none of it.

    Plain insert, deliberately NOT on_conflict_do_nothing: a conflict here
    means something the preview didn't foresee, and silently swallowing it
    would report the row as created when nothing was written. Letting it raise
    hands the row to the tiering, which isolates it and records the same
    message the row-by-row path produced.
    """
    payloads: list[dict] = []
    order: list[ImportRow] = []

    for row in rows:
        values = dict(row.normalized_payload or {})
        _apply_location_map(ctx, values)
        ref = values.get(ctx.ref_field) or external_ref_for(batch.id, row.row_number)

        payload = _model_kwargs(ctx.entity, values)
        if ctx.has_ref:
            payload.setdefault(ctx.ref_field, ref)
        payload["id"] = uuid7()
        payloads.append(payload)
        order.append(row)

    # Every dict handed to executemany must have identical keys, or SQLAlchemy
    # cannot compile one statement for the batch.
    keys: set[str] = set()
    for payload in payloads:
        keys.update(payload)
    for payload in payloads:
        for key in keys:
            payload.setdefault(key, None)

    for group in _bind_chunks(payloads, len(keys)):
        await db.execute(insert(ctx.entity.model), group)

    if ctx.entity.key == "sessions":
        # `order`, not `rows`: it is the list built alongside `payloads` in
        # the loop above, so the two are index-aligned by construction.
        await _insert_session_payments(db, ctx, order, payloads)
        await _accrue_imported_sessions(db, payloads)

    return {row.id: _Outcome(entity_id=payload["id"])
            for row, payload in zip(order, payloads)}


async def _insert_session_payments(
    db: AsyncSession, ctx: _Ctx, ordered_rows: list[ImportRow], payloads: list[dict]
) -> None:
    """One payment per imported session that carried money columns.

    Runs after the sessions are inserted, so every payment has a session_id to
    point at. Rows without an amount get no payment at all, which is what a
    historical attendance register with no billing columns should produce.

    `ordered_rows` is the list _insert_records builds alongside `payloads`, so
    the two are index-aligned and can be zipped.
    """
    payments = []
    for row, payload in zip(ordered_rows, payloads):
        values = row.normalized_payload or {}
        amount = values.get("payment_amount")
        if amount in (None, ""):
            continue
        method = values.get("payment_method")
        if not method:
            # Guarded in the validator too; this is the backstop for a row that
            # reached the writer anyway. Better a failed row than a payment
            # that cannot say who is covering it.
            raise ValueError(
                "This session has a payment amount but no payment method."
            )
        payments.append({
            "id": uuid7(),
            "session_id": payload["id"],
            "amount": Decimal(str(amount)),
            "method": method,
            "status": values.get("payment_status") or PaymentStatus.PENDING.value,
            # Dated to the session, exactly as the scheduling endpoint does.
            "date": payload["date"],
            "created_by": ctx.created_by,
        })

    for group in _bind_chunks(payments, 7):
        await db.execute(insert(Payment), group)


async def _accrue_imported_sessions(db: AsyncSession, payloads: list[dict]) -> None:
    """Give imported completed sessions the same PTO accrual a dashboard-
    completed session gets.

    The normal path goes through the sessions router, which calls
    accrue_pto_for_completed_session on the transition to COMPLETED. The
    importer bypasses that entirely — it bulk-INSERTs, which fires no ORM
    events — so a year of historical completed sessions used to land with zero
    PTO. That is why the PTO dashboard read 0h accrued against 151 sessions:
    the ledger had nothing in it.

    Built as one batched INSERT rather than a call per row, to hold the
    round-trips-per-row budget the rest of this module is written to. No
    existence check is needed: these session ids were minted moments ago in
    this same function, so nothing can already reference them.
    """
    accruals = [
        {
            "id": uuid7(),
            "therapist_id": payload["therapist_id"],
            "type": PtoTransactionType.ACCRUAL,
            "hours": ACCRUAL_RATE,
            "rate_applied": ACCRUAL_RATE,
            "source_session_id": payload["id"],
        }
        for payload in payloads
        if _is_completed(payload.get("status")) and payload.get("therapist_id")
    ]
    if not accruals:
        return
    for group in _bind_chunks(accruals, len(accruals[0])):
        await db.execute(insert(PtoTransaction), group)


def _is_completed(status) -> bool:
    """Status arrives as either the enum or its .value, depending on whether
    the payload came straight from _model_kwargs or survived a JSONB round
    trip. Compare on the value so both forms agree."""
    if status is None:
        return False
    return getattr(status, "value", status) == SessionStatus.COMPLETED.value


async def _apply_updates(
    db: AsyncSession, ctx: _Ctx, batch: ImportBatch, rows: list[ImportRow]
) -> None:
    """One batched UPDATE per table, keyed on primary key."""
    payloads: list[dict] = []
    for row in rows:
        if row.entity_id is None:
            raise ValueError("The record this row updates no longer exists")
        values = dict(row.normalized_payload or {})
        _apply_location_map(ctx, values)
        payload: dict = {"_pk": row.entity_id}
        for field_name in ((row.diff or {}).get("changes") or {}):
            spec = ctx.entity.field(field_name)
            if spec is None or spec.writable is Writability.NEVER:
                continue
            value = values.get(field_name)
            payload[model_attr(field_name, spec.kind)] = (
                uuid.UUID(value) if (spec.kind is FieldKind.FK and value)
                else _coerce(ctx.entity.model, field_name, value)
            )
        if ctx.has_ref:
            payload.setdefault(
                ctx.ref_field,
                values.get(ctx.ref_field)
                or external_ref_for(batch.id, row.row_number),
            )
        payloads.append(payload)

    keys: set[str] = set()
    for payload in payloads:
        keys.update(payload)
    keys.discard("_pk")
    for payload in payloads:
        for key in keys:
            payload.setdefault(key, None)

    if not keys:
        return
    # Table, not the ORM class — identical reasoning to _write_verdicts in
    # routers/imports.py. An ORM-enabled UPDATE carrying its own WHERE clause
    # and executed with a list of parameter sets is rejected by SQLAlchemy 2.0
    # regardless of what is in the session.
    #
    # This path is only reached when a sheet UPDATES rows that already exist,
    # which is why it went unnoticed: the differential's fixtures import into
    # an empty database, so every row is a create and this line never ran. The
    # re-import case is the whole point of the ongoing sync, so it would have
    # failed the first time a sheet was imported twice.
    table = ctx.entity.model.__table__
    statement = update(table).where(table.c.id == bindparam("_pk"))
    for group in _bind_chunks(payloads, len(keys) + 1):
        await db.execute(statement, group)


async def _apply_tiered(
    db: AsyncSession,
    ctx: _Ctx,
    batch: ImportBatch,
    rows: list[ImportRow],
    outcomes: dict[uuid.UUID, _Outcome],
    *,
    tier: int,
) -> None:
    """Try the whole set in one savepoint; narrow only where it fails.

    Per-row savepoints gave perfect isolation at the cost of three round trips
    per row. This keeps the isolation and pays for it only when something
    actually goes wrong:

        whole chunk (200)  -> one savepoint, ~5 round trips if clean
          on failure, groups of 25
            on failure, bisect                (~log2(25) ≈ 5 attempts)
              single row -> record the failure

    The preview has already validated every row against the database, so the
    clean path is the overwhelmingly common one. A group that fails costs its
    own retry, which is why the tiers narrow geometrically rather than
    dropping straight to one row at a time — isolating one bad row out of 25
    linearly would cost more than the batching saved.
    """
    if not rows:
        return

    savepoint = await db.begin_nested()
    try:
        result = await _apply_group(db, ctx, batch, rows)
        await savepoint.commit()
        outcomes.update(result)
        return
    except Exception as exc:
        await savepoint.rollback()
        if len(rows) == 1:
            logger.exception("Import row %s failed", rows[0].row_number)
            # Same humanised text the row-by-row path produced.
            outcomes[rows[0].id] = _Outcome(error=humanize(exc))
            return
        logger.info(
            "Import batch %s: group of %d failed, narrowing (%s)",
            batch.id, len(rows), exc.__class__.__name__,
        )

    tiers = _tier_sizes()
    next_tier = tier + 1
    if next_tier < len(tiers):
        size = max(1, tiers[next_tier])
        parts = [rows[i:i + size] for i in range(0, len(rows), size)]
        # A group already at or below the next tier's size would recurse
        # forever; bisect it instead.
        if len(parts) == 1:
            middle = len(rows) // 2
            parts = [rows[:middle], rows[middle:]]
    else:
        middle = len(rows) // 2
        parts = [rows[:middle], rows[middle:]]

    for part in parts:
        await _apply_tiered(db, ctx, batch, part, outcomes, tier=next_tier)


async def commit_chunk(
    db: AsyncSession, batch: ImportBatch, *, limit: int = CHUNK_SIZE
) -> CommitProgress:
    """Write one bounded slice. Call until `done`."""
    # Claim the batch BEFORE touching a single row, in its own committed
    # transaction so every other request sees it immediately.
    #
    # Previously this was set at the END of a chunk, which meant an import of
    # 200 rows or fewer never reported "committing" at all: the whole run
    # happened while the batch still said "preview", so the history listed it
    # as awaiting review and offered a Resume button for a batch actively
    # being written. It is also what makes the single-active-import check
    # possible — a claim nobody can see protects nothing.
    if batch.status != ImportStatus.COMMITTING.value:
        batch.status = ImportStatus.COMMITTING.value
        # Fixed start for the time limit. Only set when the run actually
        # begins, so resuming a timed-out batch restarts its clock rather than
        # inheriting an already-expired one.
        batch.run_started_at = datetime.now(timezone.utc)
        batch.queued_at = None
        await db.commit()

    # Fail loudly rather than holding locks. A commit that wedges — a lock
    # held by a dashboard write, a pathological query — would otherwise sit
    # there with the batch claimed and every other import blocked behind it.
    # SET LOCAL, so both die with this transaction and never leak into a
    # pooled connection someone else picks up next.
    await db.execute(text("SET LOCAL statement_timeout = '60s'"))
    await db.execute(text("SET LOCAL lock_timeout = '3s'"))

    # Second line of defence behind the `committing` status. Transaction-scoped
    # deliberately: pg_advisory_lock needs session affinity, which a
    # transaction pooler cannot promise — the lock would be taken on one
    # backend and the release attempted on another.
    # Keyed per ENTITY: a payments import and a clients import contend for
    # nothing, so they must not contend for the same lock either.
    claimed = await db.scalar(
        select(func.pg_try_advisory_xact_lock(
            _ADVISORY_LOCK_KEY, _entity_lock_key(batch.entity)
        ))
    )
    if not claimed:
        raise RuntimeError(
            "Another import is writing right now. Wait for it to finish."
        )

    entity = get_entity(batch.entity)
    ref_field = "external_id" if entity.key == "leads" else "external_ref"
    # Not every table has one. Locations are identified by name — which IS
    # unique — so they never needed a written-back reference and the migration
    # deliberately skipped them. Setting it regardless made every locations row
    # fail with "invalid keyword argument for Location".
    has_ref = ref_field in entity.model.__table__.columns

    result = await db.execute(_pending_query(batch).limit(limit))
    rows = list(result.scalars().all())

    created = updated = failed = 0

    ctx = _Ctx(
        entity=entity, ref_field=ref_field, has_ref=has_ref,
        created_by=batch.created_by,
    )
    outcomes: dict[uuid.UUID, _Outcome] = {}
    await _apply_tiered(db, ctx, batch, rows, outcomes, tier=0)

    # Statuses are written OUTSIDE every savepoint: anything set inside one
    # that later rolls back is discarded, and a row's verdict has to survive
    # its own failure.
    for row in rows:
        outcome = outcomes.get(row.id) or _Outcome(error="Row was not attempted")
        if outcome.error:
            row.status = ImportRowStatus.FAILED.value
            row.errors = [{"field": None, "column": None, "message": outcome.error}]
            failed += 1
        elif row.status == ImportRowStatus.CREATE.value:
            row.entity_id = outcome.entity_id
            row.status = ImportRowStatus.CREATED.value
            created += 1
        else:
            row.status = ImportRowStatus.UPDATED.value
            updated += 1

    batch.commit_cursor += len(rows)

    remaining = int(await db.scalar(
        select(func.count()).select_from(ImportRow).where(
            ImportRow.batch_id == batch.id, ImportRow.status.in_(PENDING)
        )
    ) or 0)

    if remaining == 0:
        batch.status = ImportStatus.COMMITTED.value
        batch.committed_at = datetime.now(timezone.utc)
        batch.run_started_at = None
        batch.last_failure = None
    else:
        batch.status = ImportStatus.COMMITTING.value

    await db.commit()

    # Finished with this entity — hand it to whoever has waited longest. Their
    # client is polling and will pick it up, so the queue drains without a
    # worker process.
    if remaining == 0:
        await promote_next_queued(db, batch.entity)

    return CommitProgress(
        processed=len(rows), created=created, updated=updated, failed=failed,
        remaining=remaining, done=remaining == 0,
    )


async def rollback_batch(db: AsyncSession, batch: ImportBatch) -> dict:
    """Undo a committed batch.

    Deletes what it created and reverts what it changed, using the before-
    values captured in each row's diff. Deliberately blunt: if someone has
    edited an imported record in the dashboard since, this puts the sheet's
    original value back. That is the right default for "this import was
    wrong, remove it", and the reason the button is admin-only and confirmed.
    """
    entity = get_entity(batch.entity)

    result = await db.execute(
        select(ImportRow)
        .where(
            ImportRow.batch_id == batch.id,
            ImportRow.status.in_((
                ImportRowStatus.CREATED.value, ImportRowStatus.UPDATED.value,
            )),
        )
        .order_by(ImportRow.row_number.desc())
    )
    rows = list(result.scalars().all())

    deleted = reverted = 0

    # Withdraw PTO accrued from the sessions this undo is about to delete.
    #
    # The accrual ledger is normally append-only — un-completing a session
    # keeps the hours, because the therapist did the work. An undone import is
    # the opposite case: those sessions never happened. Leaving the rows would
    # inflate every therapist's balance permanently, and source_session_id is
    # ON DELETE SET NULL, so they wouldn't even be traceable afterwards.
    #
    # This has to run BEFORE the delete loop below, not after. Issuing it later
    # would autoflush the loop's pending session deletes first, the FK would
    # SET NULL every source_session_id, and the IN clause would then match
    # nothing at all — a silent no-op that leaves the balances wrong.
    accruals_withdrawn = 0
    if entity.key == "sessions":
        doomed = {
            row.entity_id for row in rows
            if row.status == ImportRowStatus.CREATED.value and row.entity_id
        }
        if doomed:
            result = await db.execute(
                delete(PtoTransaction).where(
                    PtoTransaction.source_session_id.in_(doomed),
                    PtoTransaction.type == PtoTransactionType.ACCRUAL,
                )
            )
            accruals_withdrawn = result.rowcount or 0

    # Load every affected record in ONE query, not one per row.
    #
    # This was `await db.get(...)` inside the loop below — a SELECT per row,
    # issued sequentially. Undoing a 546-row leads import therefore made 546
    # round trips before it deleted anything, and at ~500ms to the database
    # that is four to five minutes during which the request simply hangs: no
    # error, no success, exactly what the rollback button was doing.
    #
    # Objects are still deleted through the ORM below rather than by a bulk
    # DELETE, so relationship cascades behave exactly as before. Only the
    # reading is batched.
    objects: dict[uuid.UUID, object] = {}
    entity_ids = [row.entity_id for row in rows if row.entity_id is not None]
    if entity_ids:
        # Chunked against the 65,535 bind-parameter ceiling, same rule the
        # commit path uses. One parameter per id, so this is generous.
        for start in range(0, len(entity_ids), 10_000):
            found = (await db.execute(
                select(entity.model).where(
                    entity.model.id.in_(entity_ids[start:start + 10_000])
                )
            )).scalars().all()
            objects.update({obj.id: obj for obj in found})

    for row in rows:
        if row.entity_id is None:
            continue
        obj = objects.get(row.entity_id)
        if obj is None:
            continue

        if row.status == ImportRowStatus.CREATED.value:
            await db.delete(obj)
            deleted += 1
        else:
            for field_name, change in ((row.diff or {}).get("changes") or {}).items():
                spec = entity.field(field_name)
                if spec is None:
                    continue
                before = change.get("from")
                setattr(
                    obj, model_attr(field_name, spec.kind),
                    uuid.UUID(before) if (spec.kind is FieldKind.FK and before) else before,
                )
            reverted += 1

        row.status = ImportRowStatus.PENDING.value
        row.entity_id = None

    batch.status = ImportStatus.ROLLED_BACK.value
    batch.commit_cursor = 0
    await db.commit()

    return {"deleted": deleted, "reverted": reverted,
            "pto_accruals_withdrawn": accruals_withdrawn}
