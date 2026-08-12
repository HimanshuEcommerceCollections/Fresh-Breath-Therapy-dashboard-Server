"""Differential: batched commit vs the original row-by-row path.

    set BENCH_IMPORT_ALLOW_WRITES=1
    python scripts/diff_import_tiers.py

For every entity the importer can write, this runs the SAME fixture sheet
through the SAME pipeline twice — once with IMPORT_CHUNK_TIER_SIZES=[1], which
reproduces the original savepoint-per-row behaviour exactly, and once with the
default tiering — then compares every written column.

Both sides go through the real upload -> preview -> commit path. There is
deliberately no hand-written "reference implementation" to compare against: a
bespoke reference is its own source of bugs, and an earlier version of the
payments test failed for exactly that reason (it fed the row-by-row side
shuffled input that the production path never sees).

Comparison excludes only what CANNOT match by construction:
  * primary keys — minted per run
  * created_at / updated_at — wall clock
  * external_ref — contains the batch id
Foreign keys are compared by the NAME of the row they point at, not by id, so
a client resolving to the wrong therapist is caught rather than hidden behind
two different UUIDs.

Everything is prefixed ZZDIFF and removed between runs, so both sides start
from an identical state. Nothing outside that prefix is touched.
"""
from __future__ import annotations

import asyncio
import csv
import io
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from sqlalchemy import delete, select, text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.client import Client  # noqa: E402
from app.models.enrollment import Enrollment  # noqa: E402
from app.models.enums import EnrollmentStatus  # noqa: E402
from app.models.import_batch import (  # noqa: E402
    ImportBatch, ImportRow, ImportStatus,
)
from app.models.lead import Lead  # noqa: E402
from app.models.location import Location  # noqa: E402
from app.models.package import Package  # noqa: E402
from app.models.payment import Payment  # noqa: E402
from app.models.session import Session  # noqa: E402
from app.models.therapist import Therapist  # noqa: E402
from app.services.importer import commit as commit_service  # noqa: E402
from app.services.importer import matcher, resolver, validator  # noqa: E402
from app.services.importer.parser import parse_sheet  # noqa: E402
from app.services.importer.registry import get_entity  # noqa: E402

P = "ZZDIFF"
FAILURES: list[str] = []


# ── fixtures ──────────────────────────────────────────────────────────────

def sheet(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue().encode()


FIXTURES: dict[str, dict] = {
    # Auto-created locations: none of these exist beforehand, so the
    # group-level resolve-or-insert path is what creates them.
    "locations": {
        "sheet": sheet(
            ["Location", "Region"],
            [[f"{P} Site {i}", "ignored"] for i in range(8)],
        ),
    },
    # Also exercises auto-created locations, from a different entity.
    "therapists": {
        "sheet": sheet(
            ["Name", "Email", "Clinic", "Credential", "Specialization", "Active"],
            [
                [f"{P} Ther {i}", f"{P.lower()}.t{i}@diff.com",
                 f"{P} Clinic {i % 3}", "LPC" if i % 2 else "LCSW",
                 "Anxiety" if i % 2 else "", "yes" if i % 3 else "no"]
                for i in range(12)
            ],
        ),
    },
    "packages": {
        "sheet": sheet(
            ["Package", "Price", "Active"],
            [[f"{P} Pkg {i}", f"{(i + 1) * 250}", "yes"] for i in range(6)],
        ),
    },
    # FK DISAMBIGUATION BY CONTEXT: two therapists share a name at different
    # locations, and each client row's own location decides which one it gets.
    "clients": {
        "sheet": sheet(
            ["Client Name", "Email", "Phone", "Therapist", "Location", "Stage"],
            [
                [f"{P} Cli {i}", f"{P.lower()}.c{i}@diff.com", "9195550100",
                 f"{P} Twin", f"{P} North" if i % 2 else f"{P} South",
                 "Ongoing" if i % 2 else "Consult done"]
                for i in range(10)
            ],
        ),
    },
    "leads": {
        "sheet": sheet(
            ["Name", "Email", "Phone", "Clinic", "Source", "Stage", "Comment",
             "Age", "Preferred"],
            [
                [f"{P} Lead {i}", f"{P.lower()}.l{i}@diff.com", "9195550101",
                 f"{P} North", "Website", "New" if i % 2 else "Called",
                 f"note {i}", str(30 + i), "Tuesday mornings"]
                for i in range(8)
            ],
        ),
    },
    "enrollments": {
        "sheet": sheet(
            ["Client", "Package", "Price", "Start", "Overdue"],
            [
                [f"{P} Cli {i}", f"{P} Pkg {i % 3}", f"{(i % 3 + 1) * 250}",
                 f"2024-0{(i % 9) + 1}-05", "no"]
                for i in range(6)
            ],
        ),
    },
    # Shuffled on purpose: the batched path must sort per enrollment in memory
    # and produce the same balance_after chain the row-by-row path produced
    # from SQL-sorted input.
    "payments": {
        "sheet": sheet(
            ["Client", "Package", "Amount", "Date", "Method"],
            [
                [f"{P} Cli 0", f"{P} Pkg 0", "100", "2024-05-02", "Cash"],
                [f"{P} Cli 0", f"{P} Pkg 0", "150", "2024-01-10", "Card"],
                [f"{P} Cli 1", f"{P} Pkg 1", "200", "2024-03-21", "Cash"],
                [f"{P} Cli 0", f"{P} Pkg 0", "125", "2024-02-05", "Cash"],
                [f"{P} Cli 1", f"{P} Pkg 1", "300", "2024-02-01", "ACH"],
            ],
        ),
    },
    # INDIRECT disambiguation: a sessions row has no location, so the twin
    # therapists are told apart by the session's CLIENT's location.
    "sessions": {
        "sheet": sheet(
            ["Client", "Therapist", "Date", "Time", "Type", "Status"],
            [
                [f"{P} Cli {i}", f"{P} Twin", f"2024-04-{(i % 27) + 1:02d}",
                 "9:00 AM" if i % 2 else "14:30",
                 "1:1" if i % 2 else "Group",
                 "Done" if i % 2 else "Booked"]
                for i in range(8)
            ],
        ),
    },
}

# Columns compared, per entity. FKs resolved to the referenced row's NAME so a
# mis-resolved foreign key is visible instead of hidden behind two UUIDs.
DUMPS: dict[str, str] = {
    "locations": f"""
        SELECT name FROM locations WHERE name LIKE '{P}%' ORDER BY name
    """,
    "therapists": f"""
        SELECT t.name, t.email, t.credential, t.specialization,
               t.employment_status, t.is_active, l.name AS location
        FROM therapists t JOIN locations l ON l.id = t.location_id
        WHERE t.name LIKE '{P}%' ORDER BY t.name
    """,
    "packages": f"""
        SELECT name, price, is_active FROM packages
        WHERE name LIKE '{P}%' ORDER BY name
    """,
    # therapist_location is the disambiguation proof.
    "clients": f"""
        SELECT c.name, c.email, c.phone, c.status,
               t.name AS therapist, tl.name AS therapist_location,
               l.name AS client_location
        FROM clients c
        JOIN therapists t ON t.id = c.therapist_id
        JOIN locations tl ON tl.id = t.location_id
        JOIN locations l ON l.id = c.location_id
        WHERE c.name LIKE '{P}%' ORDER BY c.name
    """,
    "leads": f"""
        SELECT le.name, le.email, le.phone, le.age, le.gender_or_pronoun,
               le.source, le.message, le.preferred_datetime, le.consent_given,
               le.status, l.name AS location
        FROM leads le JOIN locations l ON l.id = le.location_id
        WHERE le.name LIKE '{P}%' ORDER BY le.name
    """,
    "enrollments": f"""
        SELECT c.name AS client, p.name AS package,
               e.package_price_snapshot, e.total_paid, e.amount_due,
               e.status, e.is_overdue, e.started_at::date, e.completed_at
        FROM enrollments e
        JOIN clients c ON c.id = e.client_id
        JOIN packages p ON p.id = e.package_id
        WHERE c.name LIKE '{P}%' ORDER BY c.name, p.name, e.started_at
    """,
    "payments": f"""
        SELECT c.name AS client, p.name AS package, pay.amount_paid,
               pay.balance_after, pay.method, pay.date
        FROM payments pay
        JOIN clients c ON c.id = pay.client_id
        JOIN packages p ON p.id = pay.package_id
        WHERE c.name LIKE '{P}%'
        ORDER BY c.name, p.name, pay.date, pay.amount_paid
    """,
    "sessions": f"""
        SELECT c.name AS client, t.name AS therapist,
               tl.name AS therapist_location, s.date, s.time, s.type, s.status
        FROM sessions s
        JOIN clients c ON c.id = s.client_id
        JOIN therapists t ON t.id = s.therapist_id
        JOIN locations tl ON tl.id = t.location_id
        WHERE c.name LIKE '{P}%' ORDER BY c.name, s.date, s.time
    """,
}

NATURAL_KEY_COLS: dict[str, tuple[int, ...]] = {
    "locations": (0,),
    "therapists": (1,),            # email
    "packages": (0,),
    "clients": (1,),               # email
    "leads": (1,),
    "enrollments": (0, 1, 8),      # client, package, started_at
    "payments": (0, 1, 5, 2),      # client, package, date, amount
    "sessions": (0, 3, 4),         # client, date, time
}

ORDER = ["locations", "therapists", "packages", "clients", "leads",
         "enrollments", "payments", "sessions"]


# ── scratch state ─────────────────────────────────────────────────────────

async def wipe(db) -> None:
    """Remove every ZZDIFF row, children first. Nothing else is touched."""
    clients = (await db.execute(
        select(Client.id).where(Client.name.like(f"{P}%")))).scalars().all()
    therapists = (await db.execute(
        select(Therapist.id).where(Therapist.name.like(f"{P}%")))).scalars().all()
    packages = (await db.execute(
        select(Package.id).where(Package.name.like(f"{P}%")))).scalars().all()

    if clients:
        await db.execute(delete(Payment).where(Payment.client_id.in_(clients)))
        await db.execute(delete(Enrollment).where(Enrollment.client_id.in_(clients)))
        await db.execute(delete(Session).where(Session.client_id.in_(clients)))
    if therapists:
        await db.execute(delete(Session).where(Session.therapist_id.in_(therapists)))
    if packages:
        await db.execute(delete(Payment).where(Payment.package_id.in_(packages)))
        await db.execute(delete(Enrollment).where(Enrollment.package_id.in_(packages)))

    batches = (await db.execute(
        select(ImportBatch.id).where(ImportBatch.filename.like(f"{P}%")))).scalars().all()
    if batches:
        await db.execute(delete(ImportRow).where(ImportRow.batch_id.in_(batches)))
        await db.execute(delete(ImportBatch).where(ImportBatch.id.in_(batches)))

    await db.execute(delete(Lead).where(Lead.name.like(f"{P}%")))
    await db.execute(delete(Client).where(Client.name.like(f"{P}%")))
    await db.execute(delete(Therapist).where(Therapist.name.like(f"{P}%")))
    await db.execute(delete(Package).where(Package.name.like(f"{P}%")))
    await db.execute(delete(Location).where(Location.name.like(f"{P}%")))
    await db.commit()


async def seed(db, entity: str) -> None:
    """Prerequisites for one entity's fixture — identical for both runs."""
    need_twins = entity in ("clients", "sessions")
    need_clients = entity in ("enrollments", "payments", "sessions")
    need_packages = entity in ("enrollments", "payments")

    if entity == "leads":
        db.add(Location(id=uuid.uuid4(), name=f"{P} North"))
        await db.commit()
        return

    if not (need_twins or need_clients or need_packages):
        return

    north = Location(id=uuid.uuid4(), name=f"{P} North")
    south = Location(id=uuid.uuid4(), name=f"{P} South")
    db.add_all([north, south])
    await db.flush()

    # Two therapists, ONE name, different locations — the ambiguity the
    # context-based disambiguation has to resolve.
    twin_n = Therapist(id=uuid.uuid4(), name=f"{P} Twin",
                       email=f"{P.lower()}.twin.n@diff.com", credential="LCSW",
                       location_id=north.id)
    twin_s = Therapist(id=uuid.uuid4(), name=f"{P} Twin",
                       email=f"{P.lower()}.twin.s@diff.com", credential="LMFT",
                       location_id=south.id)
    db.add_all([twin_n, twin_s])
    await db.flush()

    if need_clients:
        for i in range(6):
            db.add(Client(
                id=uuid.uuid4(), name=f"{P} Cli {i}",
                email=f"{P.lower()}.c{i}@diff.com",
                therapist_id=(twin_n if i % 2 else twin_s).id,
                location_id=(north if i % 2 else south).id,
            ))
    if need_packages:
        for i in range(3):
            db.add(Package(id=uuid.uuid4(), name=f"{P} Pkg {i}",
                           price=Decimal(f"{(i + 1) * 250}")))
    await db.flush()

    if entity == "payments":
        await db.flush()
        clients = {c.name: c for c in (await db.execute(
            select(Client).where(Client.name.like(f"{P}%")))).scalars().all()}
        packages = {p.name: p for p in (await db.execute(
            select(Package).where(Package.name.like(f"{P}%")))).scalars().all()}
        for name, pkg in ((f"{P} Cli 0", f"{P} Pkg 0"), (f"{P} Cli 1", f"{P} Pkg 1")):
            price = packages[pkg].price
            db.add(Enrollment(
                id=uuid.uuid4(), client_id=clients[name].id,
                package_id=packages[pkg].id, package_price_snapshot=price,
                total_paid=Decimal("0"), amount_due=price,
                status=EnrollmentStatus.ACTIVE,
            ))
    await db.commit()


# ── one import, end to end ────────────────────────────────────────────────

async def run_import(db, entity_key: str, content: bytes) -> str:
    """Upload -> preview -> commit, exactly as the API does it."""
    entity = get_entity(entity_key)
    parsed = parse_sheet(content, f"{P}_{entity_key}.csv")
    proposal = matcher.propose_mapping(entity_key, parsed)

    batch = ImportBatch(
        id=uuid.uuid4(), entity=entity_key, filename=f"{P}_{entity_key}.csv",
        status=ImportStatus.MAPPING.value,
        column_mapping=proposal.as_mapping(),
        columns=[vars(c) for c in proposal.columns],
        date_order=proposal.date_order, total_rows=parsed.total_rows,
    )
    db.add(batch)
    await db.flush()
    db.add_all([
        ImportRow(id=uuid.uuid4(), batch_id=batch.id, row_number=n, raw_payload=p)
        for n, p in zip(parsed.row_numbers, parsed.rows)
    ])
    await db.commit()

    raw = [(n, p) for n, p in (await db.execute(
        select(ImportRow.row_number, ImportRow.raw_payload)
        .where(ImportRow.batch_id == batch.id)
        .order_by(ImportRow.row_number))).all()]

    prepared, prev = {}, []
    for n, p in raw:
        v, errs = validator.normalize_row(
            entity, p, batch.column_mapping, date_order=batch.date_order)
        prepared[n] = (v, errs)
        prev.append((n, v))

    fk = await resolver.resolve_foreign_keys(db, entity_key, prev)
    verdicts = await validator.validate_rows(
        db, entity_key, raw, mapping=batch.column_mapping,
        date_order=batch.date_order, fk=fk, prepared=prepared)

    stored = (await db.execute(
        select(ImportRow).where(ImportRow.batch_id == batch.id))).scalars().all()
    by_number = {v.row_number: v for v in verdicts}
    for row in stored:
        v = by_number.get(row.row_number)
        if v is None:
            continue
        row.status, row.normalized_payload = v.status, v.normalized
        row.source_hash, row.errors, row.diff = v.source_hash, v.errors or None, v.diff
        row.entity_id = uuid.UUID(v.entity_id) if v.entity_id else None
    batch.status = ImportStatus.PREVIEW.value
    await db.commit()

    while True:
        progress = await commit_service.commit_chunk(db, batch)
        if progress.done or progress.processed == 0:
            break

    counts = validator.summarize(verdicts)
    return (f"create={counts['create']} dup={counts['duplicate']} "
            f"err={counts['error']} needs={counts['needs_input']}")


async def dump(db, entity_key: str) -> list[tuple]:
    result = await db.execute(text(DUMPS[entity_key]))
    return [tuple(str(v) for v in row) for row in result.all()]


# ── the comparison ────────────────────────────────────────────────────────

async def compare(entity_key: str) -> tuple[bool, str]:
    content = FIXTURES[entity_key]["sheet"]
    runs: dict[str, list[tuple]] = {}
    summaries: dict[str, str] = {}

    for label, tiers in (("row-by-row", [1]), ("batched", [200, 25])):
        original = settings.IMPORT_CHUNK_TIER_SIZES
        settings.IMPORT_CHUNK_TIER_SIZES = tiers
        try:
            async with AsyncSessionLocal() as db:
                await wipe(db)
                await seed(db, entity_key)
                summaries[label] = await run_import(db, entity_key, content)
                runs[label] = await dump(db, entity_key)
        finally:
            settings.IMPORT_CHUNK_TIER_SIZES = original

    async with AsyncSessionLocal() as db:
        await wipe(db)

    a, b = runs["row-by-row"], runs["batched"]

    if summaries["row-by-row"] != summaries["batched"]:
        return False, (f"verdict counts differ: row-by-row {summaries['row-by-row']} "
                       f"vs batched {summaries['batched']}")
    if len(a) != len(b):
        return False, f"row count differs: {len(a)} vs {len(b)}"
    if not a:
        return False, "no rows were written by either run — fixture wrote nothing"

    key_cols = NATURAL_KEY_COLS[entity_key]
    keys_a = {tuple(r[i] for i in key_cols) for r in a}
    keys_b = {tuple(r[i] for i in key_cols) for r in b}
    if keys_a != keys_b:
        only_a = sorted(keys_a - keys_b)[:3]
        only_b = sorted(keys_b - keys_a)[:3]
        return False, f"natural keys differ. only row-by-row: {only_a}; only batched: {only_b}"

    header = [c.strip() for c in
              DUMPS[entity_key].split("SELECT", 1)[1].split("FROM")[0].split(",")]
    for index, (row_a, row_b) in enumerate(zip(a, b)):
        for col, (va, vb) in enumerate(zip(row_a, row_b)):
            if va != vb:
                name = header[col] if col < len(header) else f"col{col}"
                return False, (f"row {index + 1}, column `{name}`: "
                               f"row-by-row={va!r} batched={vb!r}")

    return True, f"{len(a)} rows identical across {len(a[0])} columns"


async def main() -> None:
    if os.getenv("BENCH_IMPORT_ALLOW_WRITES") != "1":
        print("REFUSING TO RUN — set BENCH_IMPORT_ALLOW_WRITES=1.\n"
              "This writes and deletes real rows (prefix ZZDIFF) in a database\n"
              "holding patient data.")
        sys.exit(2)

    print(f"db: {settings.db_endpoint} [{settings.db_pool_mode}]\n")
    print(f"{'entity':<14}{'result':<8}detail")
    print("-" * 78)
    for entity_key in ORDER:
        try:
            ok, detail = await compare(entity_key)
        except Exception as exc:  # a harness failure must not read as a pass
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        print(f"{entity_key:<14}{'PASS' if ok else 'FAIL':<8}{detail}")
        if not ok:
            FAILURES.append(entity_key)

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} entity/entities FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("batched commit is output-identical to row-by-row for every entity")


if __name__ == "__main__":
    asyncio.run(main())
