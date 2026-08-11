"""Spreadsheet import API.

The shape of the flow, and why it is four calls rather than one:

    POST   /api/imports                  upload -> rows stored, mapping proposed
    PATCH  /api/imports/{id}/mapping     admin corrects the mapping
    GET    /api/imports/{id}/preview     dry run: verdicts, diffs, questions
    PATCH  /api/imports/{id}/resolutions admin answers "which Sarah Chen?"
    POST   /api/imports/{id}/commit      writes a bounded slice; poll until done
    POST   /api/imports/{id}/rollback    undoes the whole batch

Nothing before `commit` writes to a domain table. Upload and preview only
populate `import_rows`, which exists so the admin can see exactly what would
happen — per row, with her own spreadsheet line numbers — and approve it.

Admin-only throughout. This writes patient records in bulk; Coordinator can
read the dashboard but must not be able to reshape it from a file.
"""
import logging
import re
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.import_batch import (
    ImportBatch, ImportRow, ImportRowStatus, ImportStatus,
)
from app.models.user import User
from app.schemas.import_batch import (
    ColumnSuggestionOut, CommitResult, CreatedRecord, CreateMissingRecord,
    EntityFieldInfo, EntityInfo, FkCandidateOut, FkGroupOut,
    FkResolutionUpdate, FkRowRefOut, ImportBatchDetail, ImportBatchSummary,
    ImportPreview, MappingUpdate, RollbackResult, RowEdit, RowPreview,
    ValueMappingOut, ValueOption,
)
from app.services.importer import commit as commit_service
from app.services.importer import matcher, normalizers, resolver, validator
from app.services.importer.commit import model_attr
from app.services.importer.normalizers import detect_date_order, suggest_enum_mapping
from app.services.importer.parser import SheetParseError, parse_sheet
from app.services.importer.registry import (
    ENTITY_ORDER, FieldKind, REGISTRY, Writability, get_entity,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/imports", tags=["imports"])

# 25 MB. A spreadsheet of FBT's whole history is a fraction of this; anything
# larger is a mistake and should fail before it is read into memory.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

_SHEETS_ID = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)")
# Google puts the active tab in the URL fragment ("#gid=123456789"). Without
# this the export silently returns the FIRST tab, so an admin who copied the
# link while looking at tab three would import tab one and never be told.
_SHEETS_GID = re.compile(r"[#&?]gid=(\d+)")


# ── helpers ───────────────────────────────────────────────────────────────

async def _get_batch(db: AsyncSession, batch_id: uuid.UUID) -> ImportBatch:
    batch = await db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return batch


async def _load_raw_rows(db: AsyncSession, batch: ImportBatch) -> list[tuple[int, dict]]:
    result = await db.execute(
        select(ImportRow.row_number, ImportRow.raw_payload)
        .where(ImportRow.batch_id == batch.id)
        .order_by(ImportRow.row_number)
    )
    return [(number, payload) for number, payload in result.all()]


async def _load_overrides(db: AsyncSession, batch: ImportBatch) -> dict[int, dict]:
    """Admin corrections typed on the review screen, keyed by row number.

    Kept apart from raw_payload so the sheet's original value stays readable —
    a fix and a cell that was always right must not look the same afterwards.
    """
    result = await db.execute(
        select(ImportRow.row_number, ImportRow.overrides)
        .where(ImportRow.batch_id == batch.id, ImportRow.overrides.isnot(None))
    )
    return {number: values for number, values in result.all() if values}


def _batch_columns(batch: ImportBatch) -> list[ColumnSuggestionOut]:
    """The stored per-column proposal, in spreadsheet order.

    `field` here stays the matcher's ORIGINAL suggestion and is deliberately
    not overwritten with the admin's edits — `column_mapping` carries those.
    Keeping them separate is what lets the UI show "this match was a close
    guess" against the suggestion while rendering the admin's own choice in
    the dropdown, instead of labelling her manual pick as a fuzzy match.
    """
    return [ColumnSuggestionOut(**raw) for raw in (batch.columns or [])]


def _batch_headers(batch: ImportBatch) -> list[str]:
    """Header order comes from the stored array, never from raw_payload keys —
    Postgres reorders jsonb object keys and would scramble the columns."""
    return [c.header for c in _batch_columns(batch)]


async def _batch_detail(
    db: AsyncSession,
    batch: ImportBatch,
    *,
    proposal: matcher.MappingProposal | None = None,
) -> ImportBatchDetail:
    """The one place ImportBatchDetail is built.

    Previously upload, fetch and mapping-update each assembled this response
    by hand, and the fetch path simply omitted `columns` — so the wizard, which
    refetches by id straight after uploading, got an empty mapping screen with
    nothing to edit. Three copies of one response shape is how that happens;
    one function is the fix.

    `proposal` is passed only right after an upload, where it is already in
    hand. Every other caller reads the copy persisted on the batch.
    """
    raw_rows = await _load_raw_rows(db, batch)

    if proposal is not None:
        columns = [ColumnSuggestionOut(**vars(c)) for c in proposal.columns]
        unmapped = proposal.unmapped_required
        date_order_confident = proposal.date_order_confident
    else:
        columns = _batch_columns(batch)
        unmapped = matcher.unmapped_required(batch.entity, batch.column_mapping or {})
        # Recomputed rather than stored: detect_date_order is deterministic and
        # cheap, and hard-coding True here meant the DMY/MDY picker — gated on
        # this in the UI — never appeared once the wizard refetched, leaving no
        # way to resolve an ambiguous date like 03/04/2024 on a dates sheet.
        samples: list = []
        for _, payload in raw_rows[:60]:
            samples.extend(payload.values())
        _, date_order_confident = detect_date_order(samples)

    return ImportBatchDetail(
        **ImportBatchSummary.model_validate(batch).model_dump(),
        column_mapping=batch.column_mapping or {},
        columns=columns,
        value_mappings=_value_mappings(batch, raw_rows),
        unmapped_required=unmapped,
        date_order_confident=date_order_confident,
        headers=_batch_headers(batch),
    )


def _value_mappings(
    batch: ImportBatch, raw_rows: list[tuple[int, dict]]
) -> list[ValueMappingOut]:
    """Distinct values per mapped enum column, with suggested targets.

    Rendered inline under the column on the mapping screen. Header mapping
    alone is only half the job — "Stage -> status" says nothing about what
    "In Progress" means, and these enums are closed sets.
    """
    entity = get_entity(batch.entity)
    saved = batch.value_mapping or {}
    out: list[ValueMappingOut] = []

    for header, field_name in (batch.column_mapping or {}).items():
        if not field_name:
            continue
        spec = entity.field(field_name)
        if spec is None or spec.kind is not FieldKind.ENUM or spec.enum_cls is None:
            continue

        counts: dict[str, int] = {}
        for _, payload in raw_rows:
            value = payload.get(header)
            if value is None or not str(value).strip():
                continue
            key = str(value).strip()
            counts[key] = counts.get(key, 0) + 1

        distinct = sorted(counts, key=lambda v: (-counts[v], v))
        suggestions = suggest_enum_mapping(distinct, spec.enum_cls)
        approved = saved.get(field_name, {})

        out.append(ValueMappingOut(
            field=field_name,
            label=spec.label,
            options=[m.value for m in spec.enum_cls],
            values=[
                ValueOption(
                    source_value=value,
                    count=counts[value],
                    mapped_to=approved.get(value, suggestions.get(value)),
                )
                for value in distinct
            ],
        ))
    return out


def _fk_group_out(group) -> FkGroupOut:
    return FkGroupOut(
        key=group.key, field=group.field, target=group.target,
        source_value=group.source_value, row_count=group.row_count,
        status=group.status,
        disambiguator=group.disambiguator,
        disambiguator_label=group.disambiguator_label,
        resolved_id=group.resolved_id, matched_by=group.matched_by,
        candidates=[FkCandidateOut(id=c.id, label=c.label) for c in group.candidates],
        suggestion=(
            FkCandidateOut(id=group.suggestion.id, label=group.suggestion.label)
            if group.suggestion else None
        ),
        rows=[FkRowRefOut(row_number=r.row_number, label=r.label) for r in group.rows],
        message=group.message,
    )


def _saved_group_decisions(batch: ImportBatch) -> dict[str, str]:
    return (batch.fk_resolutions or {}).get("groups", {})


def _saved_row_decisions(batch: ImportBatch) -> dict[str, dict[str, str]]:
    return (batch.fk_resolutions or {}).get("rows", {})


async def _fetch_linked_sheet(url: str) -> tuple[bytes, str]:
    """Pull a Google Sheet the admin pasted a link to.

    Uses the CSV export endpoint, which requires the sheet to be readable by
    the link. That is a real limitation and a deliberate one to surface:
    link-shared PHI is not something to encourage silently.

    TODO(service-account): the production path is a Google service account
    with the sheet shared to its address — no link-sharing, and it also
    unlocks writing the reference column back into the sheet. Needs
    GOOGLE_SERVICE_ACCOUNT_JSON in config before it can be wired up.
    """
    match = _SHEETS_ID.search(url)
    if not match:
        raise HTTPException(
            status_code=422,
            detail="That doesn't look like a Google Sheets link.",
        )
    export = (
        f"https://docs.google.com/spreadsheets/d/{match.group(1)}/export?format=csv"
    )
    gid = _SHEETS_GID.search(url)
    if gid:
        export += f"&gid={gid.group(1)}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(export)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Couldn't reach the sheet: {exc}")

    content_type = response.headers.get("content-type", "")
    if response.status_code != 200 or "text/html" in content_type:
        raise HTTPException(
            status_code=403,
            detail=(
                "The sheet isn't readable. In Google Sheets choose Share → "
                "General access → Anyone with the link (Viewer), then try again."
            ),
        )
    return response.content, "linked-sheet.csv"


# ── entity picker ─────────────────────────────────────────────────────────

@router.get("/entities", response_model=list[EntityInfo])
async def list_entities(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """What can be imported, in dependency order, with each one's state.

    `ready` is what stops the admin importing clients before any therapist
    exists — which would otherwise fail every row on a NOT NULL foreign key
    and look like the importer was broken.
    """
    counts: dict[str, int] = {}
    for key in ENTITY_ORDER:
        spec = REGISTRY[key]
        counts[key] = int(
            await db.scalar(select(func.count()).select_from(spec.model)) or 0
        )

    out = []
    for key in ENTITY_ORDER:
        spec = REGISTRY[key]
        blocked_by = [dep for dep in spec.depends_on if counts.get(dep, 0) == 0]
        out.append(EntityInfo(
            key=key,
            label=spec.label,
            depends_on=list(spec.depends_on),
            notes=list(spec.notes),
            existing_count=counts[key],
            ready=not blocked_by,
            blocked_by=blocked_by,
            fields=[
                EntityFieldInfo(
                    name=f.name, label=f.label, kind=f.kind.value,
                    required=f.required, writable=f.writable.value,
                    help_text=f.help_text,
                    options=[m.value for m in f.enum_cls] if f.enum_cls else None,
                )
                for f in spec.fields
            ],
        ))
    return out


# ── upload ────────────────────────────────────────────────────────────────

@router.post("", response_model=ImportBatchDetail,
             status_code=status.HTTP_201_CREATED)
async def create_import(
    entity: str = Form(...),
    file: UploadFile | None = File(default=None),
    source_url: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Upload a sheet (or link one) and get a proposed mapping back.

    Writes only to import_rows. The file itself is parsed in memory and never
    persisted to blob storage — it contains PHI, and Cloudinary (where this
    app's other uploads go) isn't covered for that.
    """
    try:
        get_entity(entity)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if file is not None:
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        filename = file.filename or "upload.csv"
    elif source_url:
        content, filename = await _fetch_linked_sheet(source_url)
    else:
        raise HTTPException(status_code=422, detail="Provide a file or a sheet link.")

    try:
        sheet = parse_sheet(content, filename)
    except SheetParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    proposal = await matcher.propose_mapping_with_model(entity, sheet)

    batch = ImportBatch(
        id=uuid.uuid4(),
        entity=entity,
        filename=filename,
        source_url=source_url,
        status=ImportStatus.MAPPING.value,
        column_mapping=proposal.as_mapping(),
        # Persisted so every later GET can render the mapping screen. Without
        # this the wizard's own post-upload refetch returned no columns and
        # the screen came up empty with nothing to edit.
        columns=[vars(c) for c in proposal.columns],
        date_order=proposal.date_order,
        total_rows=sheet.total_rows,
        created_by=current_user.id,
    )
    db.add(batch)
    await db.flush()

    db.add_all([
        ImportRow(
            id=uuid.uuid4(),
            batch_id=batch.id,
            row_number=number,
            # Keys are the sheet's own headers; values coerced to str so the
            # payload is JSONB-safe regardless of what openpyxl handed back.
            raw_payload={
                k: (None if v is None else str(v)) for k, v in row.items()
            },
            status=ImportRowStatus.PENDING.value,
        )
        for number, row in zip(sheet.row_numbers, sheet.rows)
    ])
    await db.commit()

    return await _batch_detail(db, batch, proposal=proposal)


# ── mapping ───────────────────────────────────────────────────────────────

@router.get("/{batch_id}", response_model=ImportBatchDetail)
async def get_import(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    batch = await _get_batch(db, batch_id)
    return await _batch_detail(db, batch)


@router.get("/{batch_id}/rows", response_model=list[RowPreview])
async def list_batch_rows(
    batch_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Read the stored verdict of each row — including AFTER the commit.

    Distinct from /preview, which re-validates and deliberately refuses to run
    on a committed batch. This just reports what happened: which rows were
    written, which failed, and why. Without it a finished import could tell the
    admin "10 failed" and nothing about the reason.
    """
    batch = await _get_batch(db, batch_id)

    query = select(ImportRow).where(ImportRow.batch_id == batch.id)
    if status_filter:
        wanted = [s.strip() for s in status_filter.split(",") if s.strip()]
        query = query.where(ImportRow.status.in_(wanted))

    result = await db.execute(query.order_by(ImportRow.row_number).limit(limit))
    return [
        RowPreview(
            row_number=row.row_number,
            status=row.status,
            errors=row.errors or [],
            diff=row.diff,
            # normalized first so the field-named keys (name, email) the UI
            # builds its row label from are present; raw_payload is keyed by
            # the sheet's own headers and only useful as a fallback.
            values=row.normalized_payload or row.raw_payload or {},
        )
        for row in result.scalars().all()
    ]


@router.patch("/{batch_id}/mapping", response_model=ImportBatchDetail)
async def update_mapping(
    batch_id: uuid.UUID,
    payload: MappingUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Save the admin's corrections. Rejects a mapping the schema can't take."""
    batch = await _get_batch(db, batch_id)
    if batch.status in (ImportStatus.COMMITTED.value, ImportStatus.COMMITTING.value):
        raise HTTPException(
            status_code=409, detail="This import has already been committed."
        )

    if payload.column_mapping is not None:
        allowed = set(matcher.mappable_field_names(batch.entity))
        unknown = {v for v in payload.column_mapping.values() if v and v not in allowed}
        if unknown:
            # Belt to the UI dropdown's braces. A field name that isn't in the
            # registry must never reach the commit, whatever sent it.
            raise HTTPException(
                status_code=422,
                detail=f"Unknown field(s): {', '.join(sorted(unknown))}",
            )
        duplicates = [
            v for v in set(payload.column_mapping.values())
            if v and list(payload.column_mapping.values()).count(v) > 1
        ]
        if duplicates:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Two columns are mapped to the same field: "
                    f"{', '.join(sorted(duplicates))}"
                ),
            )
        batch.column_mapping = payload.column_mapping

    if payload.value_mapping is not None:
        batch.value_mapping = payload.value_mapping
    if payload.date_order is not None:
        batch.date_order = payload.date_order
    if payload.migration_mode is not None:
        batch.migration_mode = payload.migration_mode

    batch.status = ImportStatus.MAPPING.value
    await db.commit()

    return await _batch_detail(db, batch)


@router.patch("/{batch_id}/resolutions", response_model=ImportBatchSummary)
async def update_resolutions(
    batch_id: uuid.UUID,
    payload: FkResolutionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Record the admin's foreign-key decisions, then re-run the preview.

    Merged rather than replaced so answering one question at a time doesn't
    discard the others, and so a row-level override can be added on top of a
    group answer already given.
    """
    batch = await _get_batch(db, batch_id)

    groups = dict(_saved_group_decisions(batch))
    groups.update(payload.groups)

    rows = {field: dict(answers) for field, answers in _saved_row_decisions(batch).items()}
    for field_name, answers in payload.rows.items():
        rows.setdefault(field_name, {}).update(answers)

    batch.fk_resolutions = {"groups": groups, "rows": rows}
    await db.commit()
    return ImportBatchSummary.model_validate(batch)


# ── preview ───────────────────────────────────────────────────────────────

@router.patch("/{batch_id}/rows/{row_number}", response_model=RowPreview)
async def edit_row(
    batch_id: uuid.UUID,
    row_number: int,
    payload: RowEdit,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Correct one row's values in place, and re-check it immediately.

    A single mistyped email shouldn't mean going back to the spreadsheet,
    re-exporting and re-uploading — especially when the other 149 rows are
    fine. Corrections are stored per row and layered over the sheet's cell at
    validation time; raw_payload keeps saying what the sheet said.

    Re-validates only this row and returns its new verdict, so the review
    screen updates without re-checking the whole batch.
    """
    batch = await _get_batch(db, batch_id)
    if batch.status == ImportStatus.COMMITTED.value:
        raise HTTPException(
            status_code=409, detail="This import has already been committed."
        )

    result = await db.execute(
        select(ImportRow).where(
            ImportRow.batch_id == batch.id, ImportRow.row_number == row_number
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")

    entity = get_entity(batch.entity)
    merged = dict(row.overrides or {})
    for field_name, value in payload.values.items():
        spec = entity.field(field_name)
        if spec is None or spec.writable is Writability.NEVER:
            continue
        # Clearing a correction falls back to the sheet's own cell rather
        # than storing an empty string over it.
        if value is None or (isinstance(value, str) and not value.strip()):
            merged.pop(field_name, None)
        else:
            merged[field_name] = value
    row.overrides = merged or None

    # Re-validate this row alone, through exactly the same path the preview
    # uses — so what the admin sees here can't disagree with what commit does.
    overrides = await _load_overrides(db, batch)
    overrides[row_number] = merged
    normalized, _ = validator.normalize_row(
        entity, row.raw_payload, batch.column_mapping or {},
        date_order=batch.date_order, value_mapping=batch.value_mapping,
        overrides=merged,
    )
    fk = await resolver.resolve_foreign_keys(
        db, batch.entity, [(row_number, normalized)],
        decisions=_saved_group_decisions(batch),
        row_decisions=_saved_row_decisions(batch),
    )
    verdicts = await validator.validate_rows(
        db, batch.entity, [(row_number, row.raw_payload)],
        mapping=batch.column_mapping or {},
        date_order=batch.date_order,
        value_mapping=batch.value_mapping,
        fk=fk,
        migration_mode=batch.migration_mode,
        overrides_by_row=overrides,
    )

    verdict = verdicts[0]
    row.status = verdict.status
    row.errors = verdict.errors or None
    row.normalized_payload = verdict.normalized
    row.diff = verdict.diff
    await db.commit()

    return RowPreview(
        row_number=verdict.row_number, status=verdict.status,
        errors=verdict.errors, diff=verdict.diff,
        values=verdict.normalized or {},
    )


@router.post("/{batch_id}/records", response_model=CreatedRecord,
             status_code=status.HTTP_201_CREATED)
async def create_missing_record(
    batch_id: uuid.UUID,
    payload: CreateMissingRecord,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Create a record the sheet references but the database doesn't have,
    and resolve the question it was blocking — in one step.

    Values are validated through the SAME normalizers the import itself uses,
    so a bad email typed into this form is rejected with the same message it
    would get from a spreadsheet cell. One rule, one place.
    """
    batch = await _get_batch(db, batch_id)

    try:
        target = get_entity(payload.target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    kwargs: dict = {}
    errors: list[str] = []

    for spec in target.fields:
        if spec.writable is Writability.NEVER:
            continue
        raw = payload.values.get(spec.name)

        if normalizers.is_blank(raw):
            if spec.required:
                errors.append(f"{spec.label} is required.")
            continue

        if spec.kind is FieldKind.FK:
            # The UI picks these from id-based dropdowns, so anything that
            # isn't a UUID is a client bug rather than admin input.
            try:
                kwargs[model_attr(spec.name, spec.kind)] = uuid.UUID(str(raw))
            except ValueError:
                errors.append(f"{spec.label} must be an existing record.")
            continue

        try:
            kwargs[spec.name] = normalizers.normalize_cell(
                spec, raw, date_order=batch.date_order
            )
        except normalizers.CellError as exc:
            errors.append(str(exc))

    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))

    record = target.model(id=uuid.uuid4(), **kwargs)
    db.add(record)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=commit_service.humanize(exc))

    # Answer the question this record was created for, so the admin doesn't
    # have to go back and pick it out of the list by hand.
    groups = dict(_saved_group_decisions(batch))
    groups[payload.group_key] = str(record.id)
    batch.fk_resolutions = {
        "groups": groups, "rows": _saved_row_decisions(batch),
    }

    candidate = await resolver.candidate_for(db, payload.target, record.id)
    await db.commit()
    await db.refresh(batch)

    return CreatedRecord(
        candidate=FkCandidateOut(id=candidate.id, label=candidate.label),
        batch=ImportBatchSummary.model_validate(batch),
    )


@router.get("/{batch_id}/preview", response_model=ImportPreview)
async def preview_import(
    batch_id: uuid.UUID,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    only: str | None = Query(default=None, description="Filter by row status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """The dry run. Reads the database, writes only to import_rows.

    Re-runnable: every call re-validates from the raw rows, so correcting a
    mapping or answering a foreign-key question and calling again gives an
    updated verdict without re-uploading.
    """
    batch = await _get_batch(db, batch_id)
    if batch.status == ImportStatus.COMMITTED.value:
        raise HTTPException(
            status_code=409, detail="This import has already been committed."
        )

    missing = matcher.unmapped_required(batch.entity, batch.column_mapping or {})
    raw_rows = await _load_raw_rows(db, batch)
    overrides = await _load_overrides(db, batch)

    # Row numbers travel with the values: the resolver groups by name AND by
    # the row's own context (a client's location), so it needs to know which
    # rows fed which group.
    normalized_preview = []
    for row_number, payload in raw_rows:
        values, _ = validator.normalize_row(
            get_entity(batch.entity), payload, batch.column_mapping or {},
            date_order=batch.date_order, value_mapping=batch.value_mapping,
            overrides=overrides.get(row_number),
        )
        normalized_preview.append((row_number, values))

    fk = await resolver.resolve_foreign_keys(
        db, batch.entity, normalized_preview,
        decisions=_saved_group_decisions(batch),
        row_decisions=_saved_row_decisions(batch),
    )

    verdicts = await validator.validate_rows(
        db, batch.entity, raw_rows,
        mapping=batch.column_mapping or {},
        date_order=batch.date_order,
        value_mapping=batch.value_mapping,
        fk=fk,
        migration_mode=batch.migration_mode,
        overrides_by_row=overrides,
    )

    # Persist the verdicts — this is what commit_chunk later reads.
    by_number = {v.row_number: v for v in verdicts}
    result = await db.execute(
        select(ImportRow).where(ImportRow.batch_id == batch.id)
    )
    for row in result.scalars().all():
        verdict = by_number.get(row.row_number)
        if verdict is None:
            continue
        row.status = verdict.status
        row.normalized_payload = verdict.normalized
        row.source_hash = verdict.source_hash
        row.errors = verdict.errors or None
        row.diff = verdict.diff
        row.entity_id = uuid.UUID(verdict.entity_id) if verdict.entity_id else None

    counts = validator.summarize(verdicts)
    batch.create_count = counts["create"]
    batch.update_count = counts["update"]
    batch.skip_count = counts["skip"]
    batch.error_count = counts["error"] + counts["needs_input"]
    batch.status = ImportStatus.PREVIEW.value
    await db.commit()

    blocking = fk.blocking()
    value_maps = _value_mappings(batch, raw_rows)
    unmapped_values = sum(
        1 for vm in value_maps for v in vm.values if not v.mapped_to
    )

    blockers: list[str] = []
    if missing:
        blockers.append(
            f"Required field(s) not mapped: {', '.join(missing)}"
        )
    if unmapped_values:
        # Surfaced separately because the fix isn't in the sheet — it's a
        # dropdown on the mapping screen, and a bare row error wouldn't say so.
        blockers.append(
            f"{unmapped_values} value(s) in dropdown columns aren't mapped yet"
        )
    if blocking:
        blockers.append(
            f"{len(blocking)} name(s) still need a decision before importing"
        )
    if counts["create"] + counts["update"] == 0:
        blockers.append("Nothing to import — every row is a skip or an error")

    shown = [v for v in verdicts if (only is None or v.status == only)]
    return ImportPreview(
        batch=ImportBatchSummary.model_validate(batch),
        counts=counts,
        fk_groups=[_fk_group_out(g) for g in fk.groups.values()],
        blocking_fk_count=len(blocking),
        unmapped_required=missing,
        value_mappings=value_maps,
        total_rows=len(shown),
        rows=[
            RowPreview(
                row_number=v.row_number, status=v.status,
                errors=v.errors, diff=v.diff, values=v.normalized or {},
            )
            for v in shown[offset:offset + limit]
        ],
        can_commit=not blockers,
        blockers=blockers,
    )


# ── commit / rollback ─────────────────────────────────────────────────────

@router.post("/{batch_id}/commit", response_model=CommitResult)
async def commit_import(
    batch_id: uuid.UUID,
    limit: int = Query(default=commit_service.CHUNK_SIZE, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Write one bounded slice. Call again while `done` is false.

    Chunked because this backend is serverless: a single call that tried to
    write 5,000 rows would be killed at the function timeout partway through.
    The slice is chosen by row status rather than an offset, so a call that
    dies mid-way resumes exactly where it stopped and never double-writes.
    """
    batch = await _get_batch(db, batch_id)

    if batch.status == ImportStatus.COMMITTED.value:
        return CommitResult(
            processed=0, created=0, updated=0, failed=0, remaining=0, done=True,
            batch=ImportBatchSummary.model_validate(batch),
        )
    if batch.status not in (ImportStatus.PREVIEW.value, ImportStatus.COMMITTING.value):
        raise HTTPException(
            status_code=409,
            detail="Review the preview before committing this import.",
        )

    # Re-check the blockers server-side. The UI disables the button, but the
    # button is not the guard.
    missing = matcher.unmapped_required(batch.entity, batch.column_mapping or {})
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Required field(s) not mapped: {', '.join(missing)}",
        )
    still_blocked = await db.scalar(
        select(func.count()).select_from(ImportRow).where(
            ImportRow.batch_id == batch.id,
            ImportRow.status == ImportRowStatus.NEEDS_INPUT.value,
        )
    )
    if still_blocked:
        raise HTTPException(
            status_code=422,
            detail=f"{still_blocked} row(s) still need a name resolved.",
        )

    progress = await commit_service.commit_chunk(db, batch, limit=limit)
    return CommitResult(
        processed=progress.processed, created=progress.created,
        updated=progress.updated, failed=progress.failed,
        remaining=progress.remaining, done=progress.done,
        batch=ImportBatchSummary.model_validate(batch),
    )


@router.post("/{batch_id}/rollback", response_model=RollbackResult)
async def rollback_import(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Undo a committed batch: delete what it created, revert what it changed.

    Blunt by design — if an imported record has since been edited in the
    dashboard, this puts the sheet's original value back. That is what "this
    import was wrong, remove it" has to mean.
    """
    batch = await _get_batch(db, batch_id)
    if batch.status not in (
        ImportStatus.COMMITTED.value, ImportStatus.COMMITTING.value,
    ):
        raise HTTPException(
            status_code=409, detail="This import hasn't been committed."
        )
    result = await commit_service.rollback_batch(db, batch)
    return RollbackResult(
        **result, batch=ImportBatchSummary.model_validate(batch)
    )


# ── history ───────────────────────────────────────────────────────────────

@router.get("", response_model=list[ImportBatchSummary])
async def list_imports(
    entity: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    query = select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)
    if entity:
        query = query.where(ImportBatch.entity == entity)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.delete("/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_import(
    batch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Throw away an uncommitted import. Committed ones must be rolled back
    first, so the audit trail of what was written can't be deleted out from
    under the records it explains."""
    batch = await _get_batch(db, batch_id)
    if batch.status == ImportStatus.COMMITTED.value:
        raise HTTPException(
            status_code=409,
            detail="This import was committed — roll it back before discarding it.",
        )
    await db.delete(batch)   # import_rows cascade
    await db.commit()
