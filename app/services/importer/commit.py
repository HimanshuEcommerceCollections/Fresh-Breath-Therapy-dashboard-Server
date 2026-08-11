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
  * Enrollments carry the price from the sheet, never today's list price.
  * No notifications. The lead webhook raises one per lead, which is right
    for a trickle of live enquiries and catastrophic for a 500-row import.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, Integer, Numeric, Time, func, select, text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enrollment import Enrollment
from app.models.enums import EnrollmentStatus
from app.models.import_batch import ImportBatch, ImportRow, ImportRowStatus, ImportStatus
from app.models.package import Package
from app.models.payment import Payment
from app.services.importer.registry import FieldKind, Writability, get_entity
from app.services.importer.resolver import ensure_location
from app.services.importer.validator import model_attr

logger = logging.getLogger(__name__)

# Rows per call. Small enough to stay well inside a serverless timeout even
# when every row triggers an enrollment lock, large enough that a few thousand
# rows is a handful of calls rather than hundreds.
CHUNK_SIZE = 200

PENDING = (ImportRowStatus.CREATE.value, ImportRowStatus.UPDATE.value)


@dataclass
class CommitProgress:
    processed: int
    created: int
    updated: int
    failed: int
    remaining: int
    done: bool


def external_ref_for(batch_id: uuid.UUID, row_number: int) -> str:
    """Deterministic, so re-running a batch reproduces the same references
    rather than minting new ones and duplicating every row."""
    return f"import:{batch_id}:{row_number}"


def _pending_query(batch: ImportBatch):
    query = select(ImportRow).where(
        ImportRow.batch_id == batch.id, ImportRow.status.in_(PENDING)
    )
    if batch.entity == "payments":
        # Date order, because each payment's balance_after depends on every
        # payment before it. Sheet order would produce a running balance that
        # is arithmetically correct but historically wrong.
        return query.order_by(
            text("normalized_payload->>'date'"), ImportRow.row_number
        )
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


def _model_kwargs(entity, values: dict) -> dict:
    """Normalized field values -> model constructor kwargs."""
    kwargs = {}
    for spec in entity.fields:
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

    if "uq_active_enrollment" in detail:
        return (
            "This client already has an active enrollment for this package. "
            "Complete the existing one first, or remove this row from the sheet."
        )
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


async def _create_payment(
    db: AsyncSession, values: dict, ref: str, created_by: uuid.UUID | None
) -> Payment:
    """One imported transaction, applied exactly as create_payment would.

    Locks the enrollment row for the update so a concurrent live payment
    can't read the same total and drop one of the two.
    """
    client_id = uuid.UUID(values["client"])
    package_id = uuid.UUID(values["package"])
    amount = Decimal(str(values["amount_paid"]))

    result = await db.execute(
        select(Enrollment)
        .where(
            Enrollment.client_id == client_id,
            Enrollment.package_id == package_id,
            Enrollment.status == EnrollmentStatus.ACTIVE,
        )
        .with_for_update()
    )
    enrollment = result.scalar_one_or_none()

    if enrollment is None:
        # No active cycle. Falls back to the package's current price, which is
        # why enrollments are meant to be imported first — a purchase made in
        # 2021 should carry its 2021 price, and only the enrollments sheet
        # knows what that was.
        package = await db.get(Package, package_id)
        if package is None:
            raise ValueError("Package no longer exists")
        enrollment = Enrollment(
            id=uuid.uuid4(),
            client_id=client_id,
            package_id=package_id,
            package_price_snapshot=package.price,
            total_paid=Decimal("0"),
            amount_due=package.price,
            status=EnrollmentStatus.ACTIVE,
        )
        db.add(enrollment)
        await db.flush()

    enrollment.total_paid = Decimal(str(enrollment.total_paid)) + amount
    remaining = Decimal(str(enrollment.package_price_snapshot)) - Decimal(
        str(enrollment.total_paid)
    )
    enrollment.amount_due = remaining if remaining > 0 else Decimal("0")

    if Decimal(str(enrollment.total_paid)) >= Decimal(
        str(enrollment.package_price_snapshot)
    ):
        enrollment.status = EnrollmentStatus.COMPLETED
        enrollment.completed_at = datetime.now(timezone.utc)
        enrollment.is_overdue = False

    payment = Payment(
        id=uuid.uuid4(),
        external_ref=ref,
        enrollment_id=enrollment.id,
        client_id=client_id,
        package_id=package_id,
        amount_paid=amount,
        balance_after=enrollment.amount_due,
        method=values["method"],
        date=_coerce(Payment, "date", values["date"]),
        created_by=created_by,
    )
    db.add(payment)
    return payment


async def _create_enrollment(db: AsyncSession, values: dict, ref: str) -> Enrollment:
    """An enrollment starts at zero paid — its payments then replay onto it."""
    price = Decimal(str(values["package_price_snapshot"]))
    enrollment = Enrollment(
        id=uuid.uuid4(),
        external_ref=ref,
        client_id=uuid.UUID(values["client"]),
        package_id=uuid.UUID(values["package"]),
        package_price_snapshot=price,
        total_paid=Decimal("0"),
        amount_due=price,
        status=EnrollmentStatus.ACTIVE,
        is_overdue=bool(values.get("is_overdue") or False),
    )
    # Both are timestamptz columns fed by a DATE field, and both arrive from
    # JSONB as strings — _coerce widens them to midnight UTC. started_at was
    # also simply being dropped, so every historical enrollment was landing
    # with today's date via the column's server_default.
    if values.get("started_at"):
        enrollment.started_at = _coerce(Enrollment, "started_at", values["started_at"])
    if values.get("completed_at"):
        enrollment.completed_at = _coerce(
            Enrollment, "completed_at", values["completed_at"]
        )
    db.add(enrollment)
    return enrollment


async def commit_chunk(
    db: AsyncSession, batch: ImportBatch, *, limit: int = CHUNK_SIZE
) -> CommitProgress:
    """Write one bounded slice. Call until `done`."""
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

    for row in rows:
        values = dict(row.normalized_payload or {})
        ref = values.get(ref_field) or external_ref_for(batch.id, row.row_number)
        was_create = row.status == ImportRowStatus.CREATE.value
        new_entity_id = None
        error: str | None = None

        try:
            # A SAVEPOINT per row. One bad row must not abandon the other 199,
            # and without this a failed flush leaves the session unusable for
            # everything after it.
            async with db.begin_nested():
                await _resolve_auto_create_fks(db, entity, values)

                if was_create:
                    if entity.key == "payments":
                        obj = await _create_payment(db, values, ref, batch.created_by)
                    elif entity.key == "enrollments":
                        obj = await _create_enrollment(db, values, ref)
                    else:
                        kwargs = _model_kwargs(entity, values)
                        if has_ref:
                            kwargs.setdefault(ref_field, ref)
                        obj = entity.model(id=uuid.uuid4(), **kwargs)
                        db.add(obj)
                    await db.flush()
                    new_entity_id = obj.id
                else:
                    existing = await db.get(entity.model, row.entity_id)
                    if existing is None:
                        raise ValueError(
                            "The record this row updates no longer exists"
                        )
                    changes = (row.diff or {}).get("changes") or {}
                    for field_name in changes:
                        spec = entity.field(field_name)
                        if spec is None or spec.writable is Writability.NEVER:
                            continue
                        value = values.get(field_name)
                        setattr(
                            existing,
                            model_attr(field_name, spec.kind),
                            uuid.UUID(value)
                            if (spec.kind is FieldKind.FK and value) else value,
                        )
                    if has_ref and getattr(existing, ref_field, None) is None:
                        setattr(existing, ref_field, ref)
                    await db.flush()
        except Exception as exc:
            logger.exception("Import row %s failed", row.row_number)
            # Full detail goes to the log; the row keeps a sentence the admin
            # can act on rather than a SQLAlchemy dump of the INSERT.
            error = humanize(exc)

        # Set outside the savepoint: anything written inside a rolled-back one
        # is discarded, and the row's verdict has to survive the failure.
        if error:
            row.status = ImportRowStatus.FAILED.value
            row.errors = [{"field": None, "column": None, "message": error}]
            failed += 1
        elif was_create:
            row.entity_id = new_entity_id
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
    else:
        batch.status = ImportStatus.COMMITTING.value

    await db.commit()

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
    touched_enrollments: set[uuid.UUID] = set()

    for row in rows:
        if row.entity_id is None:
            continue
        obj = await db.get(entity.model, row.entity_id)
        if obj is None:
            continue

        if row.status == ImportRowStatus.CREATED.value:
            if entity.key == "payments":
                touched_enrollments.add(obj.enrollment_id)
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

    # Deleting transactions invalidates the totals they contributed to, so
    # every affected enrollment is recomputed from the payments that remain.
    for enrollment_id in touched_enrollments:
        await _recompute_enrollment(db, enrollment_id)

    batch.status = ImportStatus.ROLLED_BACK.value
    batch.commit_cursor = 0
    await db.commit()

    return {"deleted": deleted, "reverted": reverted,
            "enrollments_recomputed": len(touched_enrollments)}


async def _recompute_enrollment(db: AsyncSession, enrollment_id: uuid.UUID) -> None:
    """Rebuild an enrollment's money from its surviving payments, in date order."""
    enrollment = await db.get(Enrollment, enrollment_id)
    if enrollment is None:
        return

    result = await db.execute(
        select(Payment)
        .where(Payment.enrollment_id == enrollment_id)
        .order_by(Payment.date, Payment.created_at)
    )
    payments = list(result.scalars().all())

    price = Decimal(str(enrollment.package_price_snapshot))
    running = Decimal("0")
    for payment in payments:
        running += Decimal(str(payment.amount_paid))
        remaining = price - running
        payment.balance_after = remaining if remaining > 0 else Decimal("0")

    enrollment.total_paid = running
    remaining = price - running
    enrollment.amount_due = remaining if remaining > 0 else Decimal("0")
    if running >= price:
        enrollment.status = EnrollmentStatus.COMPLETED
    else:
        enrollment.status = EnrollmentStatus.ACTIVE
        enrollment.completed_at = None
