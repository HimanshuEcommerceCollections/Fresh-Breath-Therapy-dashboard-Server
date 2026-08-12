"""Invariant checks for the import pipeline. Exits non-zero on failure.

    python scripts/test_import_invariants.py

Written as a standalone async script rather than pytest to match qa/test_all.py
and to avoid adding a dependency the task rules out. Every check seeds inside
a transaction and rolls it back — nothing is written.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND / ".env")

from sqlalchemy import select  # noqa: E402
from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.location import Location  # noqa: E402
from app.models.therapist import Therapist  # noqa: E402
from app.services.importer import resolver, validator  # noqa: E402
from app.services.importer.registry import get_entity  # noqa: E402

PREFIX = "ZZINV"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if not condition:
        if detail:
            print(f"        {detail}")
        FAILURES.append(name)


def make_rows(n: int) -> list[tuple[int, dict]]:
    return [
        (i + 2, {
            "Name": f"{PREFIX} P{i}",
            "Email": f"{PREFIX.lower()}.p{i}@bench.com",
            "Clinic": f"{PREFIX} Site {i % 3}",
            "Credential": "LPC",
        })
        for i in range(n)
    ]


MAPPING = {"Name": "name", "Email": "email", "Clinic": "location",
           "Credential": "credential"}


async def phase1_normalize_called_once(db) -> None:
    """Preview must normalize each row exactly N times, not 2N."""
    n = 40
    rows = make_rows(n)
    entity = get_entity("therapists")

    calls = {"count": 0}
    original = validator.normalize_row

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    validator.normalize_row = counting
    try:
        prepared, prev = {}, []
        for number, payload in rows:
            values, errors = validator.normalize_row(
                entity, payload, MAPPING, date_order="MDY")
            prepared[number] = (values, errors)
            prev.append((number, values))

        fk = await resolver.resolve_foreign_keys(db, "therapists", prev)
        with_prepared = await validator.validate_rows(
            db, "therapists", rows, mapping=MAPPING, date_order="MDY",
            fk=fk, prepared=prepared)
        after_prepared = calls["count"]

        calls["count"] = 0
        without = await validator.validate_rows(
            db, "therapists", rows, mapping=MAPPING, date_order="MDY", fk=fk)
        standalone = calls["count"]
    finally:
        validator.normalize_row = original

    check("preview normalizes each row exactly once",
          after_prepared == n, f"expected {n}, got {after_prepared}")
    check("validate_rows still works standalone (PATCH /rows/{n} path)",
          standalone == n, f"expected {n}, got {standalone}")

    def shape(verdicts):
        return json.dumps([
            {"row": v.row_number, "status": v.status, "hash": v.source_hash,
             "normalized": v.normalized, "errors": v.errors, "diff": v.diff,
             "dup": v.duplicate_source}
            for v in verdicts
        ], sort_keys=True, default=str)

    check("verdicts identical with and without `prepared`",
          shape(with_prepared) == shape(without),
          "reusing the normalize pass changed a verdict")


async def prepared_not_mutated(db) -> None:
    """validate_rows swaps FK names for ids in place — it must not do that to
    the caller's dicts, or the resolver's view of the batch changes underneath
    it and a second call sees UUIDs where it expects names."""
    rows = make_rows(5)
    entity = get_entity("therapists")
    prepared, prev = {}, []
    for number, payload in rows:
        values, errors = validator.normalize_row(
            entity, payload, MAPPING, date_order="MDY")
        prepared[number] = (values, errors)
        prev.append((number, values))

    before = json.dumps({k: v[0] for k, v in prepared.items()},
                        sort_keys=True, default=str)
    fk = await resolver.resolve_foreign_keys(db, "therapists", prev)
    await validator.validate_rows(db, "therapists", rows, mapping=MAPPING,
                                  date_order="MDY", fk=fk, prepared=prepared)
    after = json.dumps({k: v[0] for k, v in prepared.items()},
                       sort_keys=True, default=str)

    check("validate_rows does not mutate the caller's normalized rows",
          before == after)


def check_uuid7() -> None:
    """Version bits, variant bits, and monotonicity across 10,000 calls."""
    from app.services.ids import uuid7

    ids = [uuid7() for _ in range(10_000)]
    check("uuid7 sets version 7", all(u.version == 7 for u in ids))
    check("uuid7 sets RFC 4122 variant",
          all((u.bytes[8] & 0xC0) == 0x80 for u in ids))
    check("uuid7 is monotonic over 10,000 calls",
          all(a.bytes < b.bytes for a, b in zip(ids, ids[1:])))
    check("uuid7 produces no duplicates", len({str(u) for u in ids}) == 10_000)


async def check_payment_ledger_differential(db) -> None:
    """Batched payments must produce the same balance_after chain as the
    row-by-row path, from deliberately shuffled input."""
    from app.config import settings
    from app.models.client import Client
    from app.models.enrollment import Enrollment
    from app.models.package import Package
    from app.models.payment import Payment
    from app.services.importer import commit as commit_service

    loc = Location(id=uuid.uuid4(), name=f"{PREFIX} PayLoc")
    db.add(loc)
    await db.flush()
    ther = Therapist(id=uuid.uuid4(), name=f"{PREFIX} PayTher",
                     email=f"{PREFIX.lower()}.pay@bench.com", location_id=loc.id)
    pkg = Package(id=uuid.uuid4(), name=f"{PREFIX} PayPkg", price=Decimal("1000"))
    db.add_all([ther, pkg])
    await db.flush()

    # Deliberately out of date order, and summing past the package price so
    # the completion branch is exercised too.
    amounts = [("2024-05-02", "150"), ("2024-01-10", "300"),
               ("2024-03-21", "250"), ("2024-02-05", "200"),
               ("2024-06-30", "180")]

    def run(tiers):
        return tiers

    results = {}
    for label, tiers in (("row-by-row", [1]), ("batched", [200, 25])):
        original = settings.IMPORT_CHUNK_TIER_SIZES
        settings.IMPORT_CHUNK_TIER_SIZES = tiers
        try:
            client = Client(id=uuid.uuid4(), name=f"{PREFIX} Payer {label}",
                            email=f"{PREFIX.lower()}.{label.replace('-','')}@b.com",
                            therapist_id=ther.id, location_id=loc.id)
            db.add(client)
            await db.flush()
            enr = Enrollment(id=uuid.uuid4(), client_id=client.id,
                             package_id=pkg.id,
                             package_price_snapshot=Decimal("1000"),
                             total_paid=Decimal("0"), amount_due=Decimal("1000"),
                             status=commit_service.EnrollmentStatus.ACTIVE)
            db.add(enr)
            await db.flush()

            ctx = commit_service._Ctx(
                entity=get_entity("payments"), ref_field="external_ref",
                has_ref=True, created_by=None)
            rows = []
            for i, (day, amount) in enumerate(amounts):
                rows.append(type("R", (), {
                    "id": uuid.uuid4(), "row_number": i + 2,
                    "status": "create",
                    "normalized_payload": {
                        "client": str(client.id), "package": str(pkg.id),
                        "amount_paid": amount, "date": day, "method": "cash",
                    },
                    "entity_id": None, "diff": None,
                })())

            fake_batch = type("B", (), {"id": uuid.uuid4()})()
            if tiers == [1]:
                # The row-by-row path never saw shuffled input: _pending_query
                # sorts by normalized_payload->>'date' in SQL before the loop
                # ever starts. Reproducing that here is what makes this a fair
                # reference rather than a comparison against a path that
                # cannot occur.
                for row in sorted(
                    rows, key=lambda r: (r.normalized_payload["date"], r.row_number)
                ):
                    await commit_service._insert_payments(db, ctx, fake_batch, [row])
            else:
                # Batched deliberately receives them SHUFFLED — sorting per
                # enrollment in memory is the behaviour under test.
                await commit_service._insert_payments(db, ctx, fake_batch, rows)
            await db.flush()

            ledger = (await db.execute(
                select(Payment.date, Payment.amount_paid, Payment.balance_after)
                .where(Payment.client_id == client.id)
                .order_by(Payment.date)
            )).all()
            await db.refresh(enr)
            results[label] = (
                [(str(d), str(a), str(b)) for d, a, b in ledger],
                str(enr.total_paid), str(enr.amount_due), enr.status.value,
            )
        finally:
            settings.IMPORT_CHUNK_TIER_SIZES = original

    same = results["row-by-row"] == results["batched"]
    check("shuffled payments: batched ledger == row-by-row ledger", same,
          f"\n        row-by-row: {results['row-by-row']}"
          f"\n        batched   : {results['batched']}")

    chain = results["batched"][0]
    running, ok = Decimal("1000"), True
    for _, amount, balance in chain:
        running -= Decimal(amount)
        expected = running if running > 0 else Decimal("0")
        ok &= Decimal(balance) == expected
    check("balance_after decreases by each payment, in date order", ok,
          str(chain))


async def main() -> None:
    async with AsyncSessionLocal() as db:
        loc = Location(id=uuid.uuid4(), name=f"{PREFIX} Site 0")
        db.add(loc)
        await db.flush()

        print("Phase 1 — normalize once")
        await phase1_normalize_called_once(db)
        await prepared_not_mutated(db)

        print("\nPhase 2 — ids")
        check_uuid7()

        print("\nPhase 2 — payment ledger equivalence")
        await check_payment_ledger_differential(db)

        # Everything above ran inside one transaction that is now discarded,
        # so none of the fixture rows reach the database.
        await db.rollback()

    await engine.dispose()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all invariants hold")


if __name__ == "__main__":
    asyncio.run(main())
