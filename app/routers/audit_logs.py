"""Read APIs over the audit log, for the compliance screen.

ADMIN ONLY. The log names who looked at which record and when; that is
exactly the material you would not want a wider audience browsing, and it is
also the trail an insider would most like to inspect before deciding whether
they were caught.

READING THE LOG IS ITSELF AUDITED. Every endpoint here records its own read,
which means paging through the screen leaves entries. That is intended, not an
oversight: "who has been reviewing the audit trail" is a question worth being
able to answer.

APPEND ONLY. There is no POST, PATCH, PUT or DELETE in this module and there
must never be one. The single path that removes rows is
audit_service.purge_expired, which is driven by the scheduler on age alone and
is not reachable from HTTP.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.models.audit_log import AuditAction, AuditLog, AuditOutcome
from app.models.user import User
from app.schemas.audit_log import (
    AuditActorOption, AuditCount, AuditLogResponse, AuditLogSummary,
)
from app.services.audit_service import record_read
from app.services.pagination import (
    DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, apply_keyset_pagination, paginate_rows,
)

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


def _apply_filters(
    query,
    *,
    actor_user_id: uuid.UUID | None,
    action: AuditAction | None,
    entity_type: str | None,
    entity_id: uuid.UUID | None,
    outcome: AuditOutcome | None,
    request_id: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    involves_entity_id: uuid.UUID | None,
):
    if actor_user_id:
        query = query.where(AuditLog.actor_user_id == actor_user_id)
    if action:
        query = query.where(AuditLog.action == action.value)
    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if outcome:
        query = query.where(AuditLog.outcome == outcome.value)
    if request_id:
        query = query.where(AuditLog.request_id == request_id)
    if date_from:
        query = query.where(AuditLog.created_at >= date_from)
    if date_to:
        query = query.where(AuditLog.created_at <= date_to)

    if involves_entity_id:
        # THE investigation query: "was this client touched at all", matching
        # both a single-record action and any list/export whose result set
        # included them.
        #
        # entity_ids has no GIN index on purpose — the index would cost more
        # than the column it serves, for a question asked a couple of times a
        # year — so this containment test is a scan. Pair it with date_from /
        # date_to on real data; the created_at index makes the range cheap and
        # the scan then runs over that slice rather than the whole table.
        query = query.where(or_(
            AuditLog.entity_id == involves_entity_id,
            AuditLog.entity_ids.contains([str(involves_entity_id)]),
        ))
    return query


@router.get("", response_model=Page[AuditLogResponse])
async def list_audit_logs(
    actor_user_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    outcome: AuditOutcome | None = None,
    request_id: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    involves_entity_id: uuid.UUID | None = Query(
        default=None,
        description="Match records where this id was the subject OR appeared in "
                    "a returned result set. Pair with a date range on large tables.",
    ),
    cursor: str | None = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Newest-first, keyset paginated.

    Keyset rather than offset for the same reason the rest of the API uses it:
    this table only grows, and an OFFSET has to scan and discard everything it
    skips, so deep paging degrades linearly. Here it matters more than
    elsewhere, because an investigation is precisely the case that pages deep.
    """
    query = _apply_filters(
        select(AuditLog),
        actor_user_id=actor_user_id, action=action, entity_type=entity_type,
        entity_id=entity_id, outcome=outcome, request_id=request_id,
        date_from=date_from, date_to=date_to,
        involves_entity_id=involves_entity_id,
    )
    query = apply_keyset_pagination(query, AuditLog, cursor, limit)
    result = await db.execute(query)
    items, next_cursor, has_more = paginate_rows(result.scalars().all(), limit)

    await record_read(
        db, "audit_log",
        count=len(items),
        criteria={
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "action": action.value if action else None,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id else None,
            "outcome": outcome.value if outcome else None,
            "involves_entity_id": str(involves_entity_id) if involves_entity_id else None,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "limit": limit,
            "paged": bool(cursor),
        },
    )
    return Page(items=items, next_cursor=next_cursor, has_more=has_more)


# Declared BEFORE /{audit_id}: FastAPI matches in definition order, so with the
# parameterised route first "summary" would be parsed as a uuid and 422.
@router.get("/summary", response_model=AuditLogSummary)
async def audit_log_summary(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Grouped counts for the header of a log screen."""
    base = select(AuditLog)
    if date_from:
        base = base.where(AuditLog.created_at >= date_from)
    if date_to:
        base = base.where(AuditLog.created_at <= date_to)
    window = base.subquery()

    async def grouped(column) -> list[AuditCount]:
        rows = (await db.execute(
            select(column, func.count()).select_from(window).group_by(column)
            .order_by(func.count().desc())
        )).all()
        return [AuditCount(key=str(k), count=n) for k, n in rows if k is not None]

    total = (await db.execute(
        select(func.count()).select_from(window)
    )).scalar_one()
    distinct_actors = (await db.execute(
        select(func.count(func.distinct(window.c.actor_user_id))).select_from(window)
    )).scalar_one()
    bounds = (await db.execute(
        select(func.min(window.c.created_at), func.max(window.c.created_at))
        .select_from(window)
    )).one()

    summary = AuditLogSummary(
        total=total,
        by_action=await grouped(window.c.action),
        by_outcome=await grouped(window.c.outcome),
        by_entity_type=await grouped(window.c.entity_type),
        distinct_actors=distinct_actors,
        oldest=bounds[0],
        newest=bounds[1],
    )
    await record_read(
        db, "audit_log", count=total,
        criteria={
            "view": "summary",
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
    )
    return summary


@router.get("/actors", response_model=list[AuditActorOption])
async def audit_log_actors(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    """Everyone who appears in the log, for a filter dropdown.

    Read from the log itself rather than from the users table, so an actor
    whose account has since been deleted still shows up — the reason
    actor_user_id carries no foreign key.
    """
    rows = (await db.execute(
        select(
            AuditLog.actor_user_id,
            func.max(AuditLog.actor_label),
            func.max(AuditLog.actor_role),
            func.count().label("event_count"),
        )
        .group_by(AuditLog.actor_user_id)
        .order_by(func.count().desc())
        .limit(limit)
    )).all()

    options = [
        AuditActorOption(
            actor_user_id=uid, actor_label=label, actor_role=role, event_count=n
        )
        for uid, label, role, n in rows
    ]
    await record_read(db, "audit_log", count=len(options),
                      criteria={"view": "actors"})
    return options


@router.get("/{audit_id}", response_model=AuditLogResponse)
async def get_audit_log(
    audit_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin()),
):
    entry = await db.get(AuditLog, audit_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Audit record not found")
    await record_read(db, "audit_log", entity_id=audit_id, criteria={"view": "detail"})
    return entry
