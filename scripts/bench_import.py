"""Baseline the import pipeline: upload -> preview -> commit.

    python scripts/bench_import.py                 # 150, 1000, 5000 rows
    python scripts/bench_import.py --sizes 150
    python scripts/bench_import.py --commit-rows 0 # preview only, writes nothing

Fixture data is prefixed BENCH_PREFIX and deleted afterwards; the script
leaves the database exactly as it found it unless --keep is passed.

The commit phase is measured on a BOUNDED SAMPLE (--commit-rows, default 40)
rather than the whole sheet, and the per-row cost extrapolated. That is not
laziness: the current commit costs three round trips per row against a
database ~300ms away, so committing 5,000 rows to obtain a baseline would
take over an hour. Per-row cost is the number the work is judged on anyway.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import csv
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from sqlalchemy import delete, func, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.client import Client  # noqa: E402
from app.models.import_batch import (  # noqa: E402
    ImportBatch, ImportRow, ImportStatus,
)
from app.models.location import Location  # noqa: E402
from app.models.therapist import Therapist  # noqa: E402
from app.services.importer import commit as commit_service  # noqa: E402
from app.services.importer import matcher, profiling, resolver, validator  # noqa: E402
from app.services.importer.parser import parse_sheet  # noqa: E402
from app.services.importer.registry import get_entity  # noqa: E402

BENCH_PREFIX = "ZZBENCH"


def make_sheet(rows: int) -> bytes:
    """A therapists sheet: one required FK (location), one unique key (email)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Name", "Email", "Clinic", "Credential", "Active"])
    for i in range(rows):
        w.writerow([
            f"{BENCH_PREFIX} Person {i}",
            f"{BENCH_PREFIX.lower()}.{i}@bench.test".replace(".test", ".com"),
            f"{BENCH_PREFIX} Site {i % 6}",
            "LPC" if i % 2 else "LCSW",
            "yes",
        ])
    return buf.getvalue().encode()


async def cleanup(db) -> int:
    """Remove everything this script created. Prefix-scoped, nothing else."""
    removed = 0
    batches = (await db.execute(
        select(ImportBatch.id).where(ImportBatch.filename.like(f"{BENCH_PREFIX}%"))
    )).scalars().all()
    if batches:
        await db.execute(delete(ImportRow).where(ImportRow.batch_id.in_(batches)))
        await db.execute(delete(ImportBatch).where(ImportBatch.id.in_(batches)))
        removed += len(batches)
    for model, column in ((Therapist, Therapist.name), (Client, Client.name),
                          (Location, Location.name)):
        result = await db.execute(
            delete(model).where(column.like(f"{BENCH_PREFIX}%"))
        )
        removed += result.rowcount or 0
    await db.commit()
    return removed


async def bench(rows: int, commit_rows: int) -> None:
    content = make_sheet(rows)
    entity_key = "therapists"

    async with AsyncSessionLocal() as db:
        with profiling.profile() as prof:

            # ── upload ────────────────────────────────────────────────────
            with profiling.phase("parse", rows):
                sheet = parse_sheet(content, f"{BENCH_PREFIX}_{rows}.csv")

            with profiling.phase("propose_mapping", rows):
                proposal = matcher.propose_mapping(entity_key, sheet)

            with profiling.phase("persist rows (upload)", rows):
                batch = ImportBatch(
                    id=uuid.uuid4(), entity=entity_key,
                    filename=f"{BENCH_PREFIX}_{rows}.csv",
                    status=ImportStatus.MAPPING.value,
                    column_mapping=proposal.as_mapping(),
                    columns=[vars(c) for c in proposal.columns],
                    date_order=proposal.date_order, total_rows=sheet.total_rows,
                )
                db.add(batch)
                await db.flush()
                db.add_all([
                    ImportRow(id=uuid.uuid4(), batch_id=batch.id,
                              row_number=number, raw_payload=payload)
                    for number, payload in zip(sheet.row_numbers, sheet.rows)
                ])
                await db.commit()

            # ── preview ───────────────────────────────────────────────────
            entity = get_entity(entity_key)
            mapping = batch.column_mapping

            with profiling.phase("load raw rows", rows):
                raw = [(n, p) for n, p in (await db.execute(
                    select(ImportRow.row_number, ImportRow.raw_payload)
                    .where(ImportRow.batch_id == batch.id)
                    .order_by(ImportRow.row_number)
                )).all()]

            with profiling.phase("normalize (once)", rows):
                prepared: dict = {}
                prev = []
                for n, p in raw:
                    v, errs = validator.normalize_row(
                        entity, p, mapping, date_order=batch.date_order)
                    prepared[n] = (v, errs)
                    prev.append((n, v))

            with profiling.phase("resolve_fks", rows):
                fk = await resolver.resolve_foreign_keys(db, entity_key, prev)

            with profiling.phase("validate (reuses normalize)", rows):
                verdicts = await validator.validate_rows(
                    db, entity_key, raw, mapping=mapping,
                    date_order=batch.date_order, fk=fk, prepared=prepared)

            with profiling.phase("persist verdicts", rows):
                by_number = {v.row_number: v for v in verdicts}
                stored = (await db.execute(
                    select(ImportRow).where(ImportRow.batch_id == batch.id)
                )).scalars().all()
                for row in stored:
                    v = by_number.get(row.row_number)
                    if v is None:
                        continue
                    row.status = v.status
                    row.normalized_payload = v.normalized
                    row.source_hash = v.source_hash
                    row.errors = v.errors or None
                    row.diff = v.diff
                batch.status = ImportStatus.PREVIEW.value
                await db.commit()

            # ── commit (bounded sample) ───────────────────────────────────
            per_row = None
            if commit_rows > 0:
                with profiling.phase(f"commit_chunk ({commit_rows} rows)", commit_rows):
                    t0 = time.perf_counter()
                    await commit_service.commit_chunk(db, batch, limit=commit_rows)
                    per_row = (time.perf_counter() - t0) / commit_rows

        counts = validator.summarize(verdicts)
        print(prof.table(f"\n=== {rows:,} rows — {entity_key} ==="))
        print(f"  verdicts: create={counts['create']} dup={counts['duplicate']} "
              f"error={counts['error']}")
        if per_row:
            sample = next(p for p in prof.phases if p.name.startswith("commit_chunk"))
            print(f"  commit  : {per_row * 1000:.0f} ms/row, "
                  f"{sample.queries / commit_rows:.1f} round trips/row")
            for n in (150, 1000, 5000):
                print(f"            projected {n:>5} rows: {per_row * n / 60:6.1f} min")

        async with AsyncSessionLocal() as clean:
            await cleanup(clean)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[150, 1000, 5000])
    ap.add_argument("--commit-rows", type=int, default=40,
                    help="rows to actually write when measuring commit; 0 = skip")
    ap.add_argument("--keep", action="store_true", help="don't clean up fixtures")
    args = ap.parse_args()

    profiling.install(engine)
    import platform, os
    where = os.getenv("VERCEL_REGION") or f"dev machine ({platform.node()})"
    print(f"run from: {where}")
    print(f"db: mode={settings.db_pool_mode} endpoint={settings.db_endpoint} "
          f"pool={settings.DB_POOL_SIZE}+{settings.DB_MAX_OVERFLOW} "
          f"pre_ping={settings.DB_POOL_PRE_PING}")

    async with AsyncSessionLocal() as db:
        t = time.perf_counter()
        await db.execute(select(func.now()))
        print(f"round-trip latency: {(time.perf_counter() - t) * 1000:.0f} ms")

    try:
        for size in args.sizes:
            await bench(size, args.commit_rows)
    finally:
        if not args.keep:
            async with AsyncSessionLocal() as db:
                removed = await cleanup(db)
            print(f"\ncleaned up {removed} fixture rows/batches")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
