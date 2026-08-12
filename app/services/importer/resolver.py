"""Turning the names in a spreadsheet into foreign keys.

Diane's sheets refer to people and things the only way a human can — by name.
"Sarah Chen", "Anxiety Package", "Cary". The database refers to them by UUID.

The hard case is a name that matches more than one record, and the naive fix
— one decision per distinct name — is quietly wrong. If five clients name
"Sarah Chen" and three of them see the Sarah Chen at Greensboro while two see
the one at Downtown, a single choice files two patients under the wrong
clinician, and nothing downstream ever flags it.

So resolution happens at three levels, cheapest first:

  1. NAME           unique match -> resolved, nothing to ask
  2. NAME + CONTEXT the row's own location narrows the candidates. Five rows
                    become "the three at Greensboro" and "the two at
                    Downtown", and each side usually resolves on its own.
                    Reported as `matched_by` so the inference is visible and
                    overridable, never silent.
  3. ROW            the admin overrides individual rows when even that isn't
                    enough (two therapists of the same name at the same
                    location — rare, but it must be expressible).

Every group carries the rows that feed it, so the UI can expand any decision
into its individual rows.

This is deliberately plain SQL, not a model call. It is a lookup, it must be
exact, and a confidently-wrong match here assigns a patient to the wrong
clinician.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field as dc_field
from difflib import get_close_matches

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.client import Client
from app.models.location import Location
from app.models.package import Package
from app.models.therapist import Therapist
from app.services.importer.registry import FieldKind, FieldSpec, get_entity

# Columns a name in a sheet is matched against, per target entity. Email is
# included where the model has one because a "Therapist" column sometimes
# holds an address rather than a name.
LOOKUP_COLUMNS = {
    "locations": (Location.name,),
    "therapists": (Therapist.name, Therapist.email),
    "clients": (Client.name, Client.email),
    "packages": (Package.name,),
}

MODELS = {
    "locations": Location,
    "therapists": Therapist,
    "clients": Client,
    "packages": Package,
}

# How to read the disambiguating attribute off an existing record, so it can
# be compared with the value on the import row. Keyed by target entity.
DISAMBIGUATOR_OF = {
    "therapists": lambda t: getattr(t.location, "name", None),
    "clients": lambda c: c.email,
}

# How many records to offer when a name matched nothing and the admin has to
# pick from the existing list. Bounded because the picker is searchable — an
# unbounded pool would pull a whole table to populate a dropdown, which is the
# exact cost Phase 5 exists to remove.
CANDIDATE_POOL = 500


class FkStatus:
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"
    WILL_CREATE = "will_create"


@dataclass
class FkCandidate:
    id: str
    label: str          # "Sarah Chen · LCSW · Cary" — enough to tell two apart


@dataclass
class FkRowRef:
    """One import row inside a group, for the per-row override list."""
    row_number: int
    label: str          # how the admin recognises it: client name or email


@dataclass
class FkGroup:
    key: str            # stable id the admin's answer is stored against
    field: str          # the field on the entity being imported
    target: str         # registry key of the referenced entity
    source_value: str   # the name exactly as it appears in the sheet
    row_count: int
    status: str
    # The context value that split this group out of a larger one — e.g. the
    # location the rows in it belong to. None when the name wasn't ambiguous.
    disambiguator: str | None = None
    disambiguator_label: str | None = None
    resolved_id: str | None = None
    # Set when the disambiguator did the resolving rather than the name alone.
    # Surfaced in the UI so an inference is always visible and reversible.
    matched_by: str | None = None
    candidates: list[FkCandidate] = dc_field(default_factory=list)
    suggestion: FkCandidate | None = None
    rows: list[FkRowRef] = dc_field(default_factory=list)
    message: str | None = None

    @property
    def needs_input(self) -> bool:
        return self.status in (FkStatus.AMBIGUOUS, FkStatus.MISSING)


@dataclass
class FkResolution:
    groups: dict[str, FkGroup]
    # (field, row_number) -> resolved id, the WILL_CREATE sentinel, or None
    # when the row is still blocked. The validator reads this directly rather
    # than rebuilding group keys, so the two can never disagree about which
    # group a row belongs to.
    row_assignments: dict[tuple[str, int], str | None]
    # (field, row_number) -> group key, so a blocked row can name its question.
    row_groups: dict[tuple[str, int], str] = dc_field(default_factory=dict)

    WILL_CREATE = "__will_create__"

    def blocking(self) -> list[FkGroup]:
        return [g for g in self.groups.values() if g.needs_input]


def _label(target: str, row) -> str:
    """How a candidate reads in a picker.

    Carries whatever actually separates two records of the same name — for a
    therapist that's credential and location, for a client it's who they see
    and where. A bare name is useless in the one situation these labels exist
    for, which is two records sharing it.
    """
    if target == "therapists":
        parts = [row.name, row.credential, getattr(row.location, "name", None)]
    elif target == "clients":
        parts = [
            row.name,
            getattr(row.therapist, "name", None),
            getattr(row.location, "name", None),
        ]
    elif target == "leads":
        parts = [row.name, row.email, getattr(row.location, "name", None)]
    elif target == "packages":
        parts = [row.name, f"${row.price}"]
    else:
        parts = [row.name]
    return " · ".join(str(p) for p in parts if p)


def _row_label(values: dict) -> str:
    for key in ("name", "email", "client", "package"):
        value = values.get(key)
        if value:
            return str(value)
    return "(row)"


def _norm(value) -> str:
    return str(value or "").strip().lower()


async def _load_index(
    db: AsyncSession, target: str, wanted: set[str] | None = None,
    *, limit: int | None = None,
) -> dict[str, list]:
    """Existing rows of `target`, keyed by each of their lookup values.

    One query per referenced entity for the whole batch, rather than a query
    per row. Two Sarah Chens land in the same list, which is precisely the
    ambiguity this module exists to resolve.

    `wanted` bounds it to the names the sheet actually mentions. Without it
    this pulled the ENTIRE table: fine against 150 therapists, ruinous against
    50,000, and the cost grew with the clinic rather than with the import. The
    round-trip count is unchanged — it is still exactly one query — but the
    rows crossing the wire are now proportional to the sheet.

    A near-miss suggestion for a name that matches nothing still needs
    candidates to compare against, so the caller widens `wanted` when it has
    unmatched names; see resolve_foreign_keys.
    """
    model = MODELS[target]
    query = select(model)
    if limit is not None:
        query = query.limit(limit)
    if wanted:
        lowered = {w.strip().lower() for w in wanted if w and w.strip()}
        if lowered:
            conditions = [
                func.lower(column).in_(lowered) for column in LOOKUP_COLUMNS[target]
            ]
            query = query.where(or_(*conditions))
    if target == "therapists":
        # location is both part of the label and the disambiguator.
        query = query.options(selectinload(Therapist.location))
    elif target == "clients":
        # location: a session row carries no location of its own, so the
        # therapist is disambiguated THROUGH the client's location.
        # therapist: part of the label, so two same-named clients can be told
        # apart in a picker.
        # Both eager-loaded — reading either lazily inside the async resolve
        # loop would raise.
        query = query.options(
            selectinload(Client.location), selectinload(Client.therapist)
        )

    result = await db.execute(query)
    index: dict[str, list] = {}
    for row in result.scalars().all():
        for column in LOOKUP_COLUMNS[target]:
            value = getattr(row, column.key, None)
            if value:
                index.setdefault(_norm(value), []).append(row)
    return index


def _candidates(target: str, matches: list) -> list[FkCandidate]:
    return [FkCandidate(id=str(m.id), label=_label(target, m)) for m in matches]


def _read_attr(obj, attr: str) -> str:
    """Read a disambiguating attribute off a record as a comparable string."""
    value = getattr(obj, attr, None)
    if value is None:
        return ""
    # Relationships (client.location) compare on their name.
    return str(getattr(value, "name", value) or "").strip()


def _context_value(entity, spec: FieldSpec, values: dict, indexes: dict) -> str:
    """The value used to tell two same-named records apart, for one row.

    Two forms:

      "location"        a sibling column on the same import row. Clients and
                        leads both carry one.
      "client.location" INDIRECT — the row has no location, but the record it
                        points at does. A sessions sheet is client + therapist
                        + when; the session's client is already in the database
                        with a location, and that is what separates two
                        therapists of the same name. Without this, a sessions
                        import asks about "Sarah Chen" even though the client's
                        own location already answers it.

    Returns "" when the context can't be established (the intermediate record
    is itself ambiguous or missing), which correctly falls through to asking.
    """
    if not spec.fk_disambiguator:
        return ""

    if "." not in spec.fk_disambiguator:
        return str(values.get(spec.fk_disambiguator) or "").strip()

    via_name, attr = spec.fk_disambiguator.split(".", 1)
    via_spec = entity.field(via_name)
    if via_spec is None or via_spec.fk_entity is None:
        return ""

    written = values.get(via_name)
    if written is None or not str(written).strip():
        return ""

    matches = indexes.get(via_spec.fk_entity, {}).get(_norm(written), [])
    if len(matches) != 1:
        # The intermediate record is ambiguous or absent, so it can't be
        # leaned on to resolve this one.
        return ""
    return _read_attr(matches[0], attr)


def _disambiguator_label(entity, spec: FieldSpec) -> str:
    """Human name for whatever is doing the disambiguating."""
    if not spec.fk_disambiguator:
        return ""
    if "." in spec.fk_disambiguator:
        return spec.fk_disambiguator.split(".", 1)[1].replace("_", " ")
    field = entity.field(spec.fk_disambiguator)
    return (field.label if field else spec.fk_disambiguator).lower()


async def resolve_foreign_keys(
    db: AsyncSession,
    entity_key: str,
    rows: list[tuple[int, dict]],
    *,
    decisions: dict[str, str] | None = None,
    row_decisions: dict[str, dict[str, str]] | None = None,
) -> FkResolution:
    """Resolve every FK reference in the batch.

    `rows` is (row_number, normalized values) — normalize_cell deliberately
    leaves FK fields as the written name, so grouping happens here where it
    can be done once for the whole batch.

    `decisions` is {group_key: uuid} and `row_decisions` is
    {field: {row_number: uuid}} — the admin's saved answers, replayed first so
    re-running the preview never re-asks a settled question. A row-level
    answer always beats its group's.
    """
    entity = get_entity(entity_key)
    fk_specs = [f for f in entity.fields if f.kind is FieldKind.FK]
    if not fk_specs:
        return FkResolution(groups={}, row_assignments={})

    decisions = decisions or {}
    row_decisions = row_decisions or {}

    # Collect the names the sheet mentions FIRST, so each entity is fetched
    # bounded by the import rather than by the size of the table.
    wanted: dict[str, set[str]] = {}
    for spec in fk_specs:
        bucket = wanted.setdefault(spec.fk_entity, set())
        for _, values in rows:
            raw = values.get(spec.name)
            if raw is not None and str(raw).strip():
                bucket.add(str(raw).strip())

    targets = {spec.fk_entity for spec in fk_specs}
    indexes = {
        target: await _load_index(db, target, wanted.get(target))
        for target in targets
    }

    # A name the sheet mentions that matched nothing still needs two things
    # the bounded index cannot supply: a list of records to pick from, and a
    # near-miss suggestion to compare against. Both need the wider table — so
    # for those targets ONLY, and only when something actually failed to
    # match, load a capped pool. A clean import never pays for this.
    pools: dict[str, dict[str, list]] = {}
    for target in targets:
        unmatched = [
            name for name in wanted.get(target, set())
            if _norm(name) not in indexes[target]
        ]
        if unmatched:
            pools[target] = await _load_index(db, target, None, limit=CANDIDATE_POOL)

    groups: dict[str, FkGroup] = {}
    row_assignments: dict[tuple[str, int], str | None] = {}
    row_groups: dict[tuple[str, int], str] = {}

    for spec in fk_specs:
        index = indexes.get(spec.fk_entity, {})
        read_disambiguator = DISAMBIGUATOR_OF.get(spec.fk_entity)

        # Bucket the rows: name -> disambiguator value -> [(row_number, values)]
        buckets: dict[str, dict[str, list[tuple[int, dict]]]] = {}
        for row_number, values in rows:
            raw = values.get(spec.name)
            if raw is None or not str(raw).strip():
                continue
            name = str(raw).strip()
            context = _context_value(entity, spec, values, indexes)
            buckets.setdefault(name, {}).setdefault(context, []).append(
                (row_number, values)
            )

        for name, by_context in buckets.items():
            matches = index.get(_norm(name), [])
            all_rows = [entry for entries in by_context.values() for entry in entries]

            # ── the name alone is enough ──────────────────────────────────
            if len(matches) == 1:
                _emit(
                    groups, row_assignments, row_groups, spec, name, None,
                    all_rows, FkStatus.RESOLVED, resolved_id=str(matches[0].id),
                    decisions=decisions, row_decisions=row_decisions,
                )
                continue

            # ── nothing matched ───────────────────────────────────────────
            if not matches:
                if spec.fk_auto_create:
                    _emit(
                        groups, row_assignments, row_groups, spec, name, None,
                        all_rows, FkStatus.WILL_CREATE,
                        message=f'No "{name}" yet — it will be created.',
                        decisions=decisions, row_decisions=row_decisions,
                    )
                    continue

                pool = pools.get(spec.fk_entity, index)
                near = get_close_matches(_norm(name), list(pool.keys()), n=1, cutoff=0.75)
                suggestion = None
                if near and pool[near[0]]:
                    match = pool[near[0]][0]
                    suggestion = FkCandidate(
                        id=str(match.id), label=_label(spec.fk_entity, match)
                    )
                _emit(
                    groups, row_assignments, row_groups, spec, name, None,
                    all_rows, FkStatus.MISSING,
                    candidates=_candidates(spec.fk_entity, _unique(pool))[:200],
                    suggestion=suggestion,
                    message=(
                        f'No {spec.fk_entity[:-1]} named "{name}". Pick one, or '
                        f"import {spec.fk_entity} first."
                    ),
                    decisions=decisions, row_decisions=row_decisions,
                )
                continue

            # ── more than one match: split on the row's own context ───────
            if not spec.fk_disambiguator or read_disambiguator is None:
                _emit(
                    groups, row_assignments, row_groups, spec, name, None,
                    all_rows, FkStatus.AMBIGUOUS,
                    candidates=_candidates(spec.fk_entity, matches),
                    message=f"{len(matches)} records share this name — pick the right one.",
                    decisions=decisions, row_decisions=row_decisions,
                )
                continue

            context_label = _disambiguator_label(entity, spec)

            for context, entries in by_context.items():
                narrowed = [
                    m for m in matches if _norm(read_disambiguator(m)) == _norm(context)
                ] if context else []

                if len(narrowed) == 1:
                    # The context settled it. Reported, not silent.
                    _emit(
                        groups, row_assignments, row_groups, spec, name, context,
                        entries, FkStatus.RESOLVED,
                        resolved_id=str(narrowed[0].id),
                        matched_by=context_label.lower(),
                        candidates=_candidates(spec.fk_entity, matches),
                        decisions=decisions, row_decisions=row_decisions,
                    )
                    continue

                # The context couldn't settle it. Three distinct situations,
                # and saying which one it is tells the admin what to look at:
                #   - no context on the row at all
                #   - several same-named records share the context
                #   - none of them match the context
                # The candidate list narrows to those at the location when
                # that set is non-empty, so the choice stays as small as the
                # data allows.
                noun = spec.fk_entity[:-1]
                label = context_label
                if not context:
                    message = (
                        f"{len(matches)} {spec.fk_entity} share this name and "
                        f"these rows have no {label} — choose for each row."
                    )
                elif len(narrowed) > 1:
                    message = (
                        f"{len(narrowed)} of them work at “{context}”, so the "
                        f"{label} can't decide it — choose for each row."
                    )
                else:
                    message = (
                        f"No {noun} with this name works at “{context}” — "
                        f"choose for each row."
                    )

                _emit(
                    groups, row_assignments, row_groups, spec, name, context,
                    entries, FkStatus.AMBIGUOUS,
                    candidates=_candidates(spec.fk_entity, narrowed or matches),
                    message=message,
                    decisions=decisions, row_decisions=row_decisions,
                )

    return FkResolution(
        groups=groups, row_assignments=row_assignments, row_groups=row_groups
    )


def _emit(
    groups: dict[str, FkGroup],
    row_assignments: dict[tuple[str, int], str | None],
    row_groups: dict[tuple[str, int], str],
    spec: FieldSpec,
    name: str,
    context: str | None,
    entries: list[tuple[int, dict]],
    status: str,
    *,
    resolved_id: str | None = None,
    matched_by: str | None = None,
    candidates: list[FkCandidate] | None = None,
    suggestion: FkCandidate | None = None,
    message: str | None = None,
    decisions: dict[str, str],
    row_decisions: dict[str, dict[str, str]],
) -> None:
    """Record one group and the assignment of every row inside it."""
    key = f"{spec.name}::{name}" + (f"::{context}" if context else "")

    # A saved group answer overrides whatever we worked out.
    saved = decisions.get(key)
    if saved:
        status, resolved_id, matched_by = FkStatus.RESOLVED, saved, None

    group = FkGroup(
        key=key, field=spec.name, target=spec.fk_entity, source_value=name,
        row_count=len(entries), status=status,
        disambiguator=context or None,
        # The attribute name, never the lookup path — "location", not
        # "client.location". The path is an implementation detail of how we
        # reached it and means nothing to whoever reads the screen.
        disambiguator_label=(
            spec.fk_disambiguator.split(".")[-1].replace("_", " ")
            if spec.fk_disambiguator else None
        ),
        resolved_id=resolved_id, matched_by=matched_by,
        candidates=candidates or [],
        suggestion=suggestion,
        rows=[FkRowRef(row_number=n, label=_row_label(v)) for n, v in entries],
        message=message,
    )
    groups[key] = group

    per_row = row_decisions.get(spec.name, {})
    for row_number, _ in entries:
        row_groups[(spec.name, row_number)] = key
        override = per_row.get(str(row_number))
        if override:
            row_assignments[(spec.name, row_number)] = override
        elif status == FkStatus.RESOLVED:
            row_assignments[(spec.name, row_number)] = resolved_id
        elif status == FkStatus.WILL_CREATE:
            row_assignments[(spec.name, row_number)] = FkResolution.WILL_CREATE
        else:
            row_assignments[(spec.name, row_number)] = None

    # A group whose every row has been answered individually isn't a question
    # any more, however unresolved the group itself looks.
    if group.needs_input and all(
        row_assignments[(spec.name, n)] for n, _ in entries
    ):
        group.status = FkStatus.RESOLVED
        group.matched_by = "set per row"


def _unique(index: dict[str, list]) -> list:
    """Flatten a lookup index back to unique rows."""
    seen: dict[str, object] = {}
    for rows in index.values():
        for row in rows:
            seen[str(row.id)] = row
    return list(seen.values())


async def candidate_for(
    db: AsyncSession, target: str, record_id: uuid.UUID
) -> FkCandidate:
    """Label one record the same way the resolver labels its candidates.

    Used after creating a missing record inline, so the newly added row reads
    identically to one that was already there ("Sarah Chen · LCSW · Cary")
    rather than appearing as a bare name the admin can't place.
    """
    model = MODELS[target]
    query = select(model).where(model.id == record_id)
    if target == "therapists":
        query = query.options(selectinload(Therapist.location))
    elif target == "clients":
        query = query.options(
            selectinload(Client.location), selectinload(Client.therapist)
        )
    record = (await db.execute(query)).scalar_one()
    return FkCandidate(id=str(record.id), label=_label(target, record))


async def ensure_location(db: AsyncSession, name: str) -> uuid.UUID:
    """Location by name, created if new.

    Mirrors _resolve_location in the lead webhook — same rule, same reasoning:
    a misfiled or rejected record costs more than a duplicate the admin can
    delete in one click.
    """
    cleaned = (name or "").strip() or "Unspecified"
    result = await db.execute(
        select(Location).where(func.lower(Location.name) == func.lower(cleaned))
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing.id

    location = Location(id=uuid.uuid4(), name=cleaned)
    db.add(location)
    await db.flush()
    return location.id
