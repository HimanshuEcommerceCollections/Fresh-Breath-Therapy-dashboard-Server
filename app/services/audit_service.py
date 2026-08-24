"""Audit entries that the ORM cannot infer.

Writes are captured automatically by audit_listener. Everything here is the
remainder — the events that leave no trace in the database on their own:

  * READS. A select changes nothing, so there is no flush and no hook. Item
    3.3 still wants them, and they are the entries an investigator asks for
    first: viewing a record IS the access event. This is the one place manual
    discipline is unavoidable, so it is one call per PHI route and coverage is
    verified route by route rather than assumed.
  * EXPORTS (item 3.6), with row count and filter criteria, because bulk
    extraction is the highest-risk action the system offers.
  * DENIED ATTEMPTS (item 3.5) — a refusal is often the more interesting
    signal than a success.
  * AUTH EVENTS: login, failed login, logout.
  * The retention purge, which audits itself.

FAILURE POLICY. Nothing here swallows an exception. A read audit row is added
to the session the request ALREADY holds and committed on it, so "the audit
write failed" and "this request's own database work failed" are the same event
on the same connection — fail-closed costs nothing because it is not a separate
failure domain. The pattern item 3.10 exists to forbid is
`try: audit() except: pass`, and it appears nowhere in this module.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.middleware.request_id import get_request_id
from app.models.audit_log import (
    MAX_ENTITY_IDS, AuditAction, AuditLog, AuditOutcome,
)
from app.services.audit_context import AuditContext, current_context

logger = logging.getLogger(__name__)


def _cap_ids(entity_ids) -> tuple[list[str] | None, int | None, bool]:
    """(ids, count, truncated).

    The count is always the TRUE total even when the id list is clipped, so a
    truncated row still answers "how many records did this return" — which is
    what item 3.6 asks of an export.
    """
    if entity_ids is None:
        return None, None, False
    ids = [str(i) for i in entity_ids if i is not None]
    total = len(ids)
    if total > MAX_ENTITY_IDS:
        return ids[:MAX_ENTITY_IDS], total, True
    return ids, total, False


def build_entry(
    action: AuditAction,
    entity_type: str,
    *,
    entity_id: uuid.UUID | None = None,
    entity_ids=None,
    count: int | None = None,
    criteria: dict | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    status_code: int | None = None,
    context: AuditContext | None = None,
) -> AuditLog:
    ids, derived_count, truncated = _cap_ids(entity_ids)
    ctx = context or current_context()
    return AuditLog(
        id=uuid.uuid4(),
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_ids=ids,
        entity_count=count if count is not None else derived_count,
        truncated=truncated,
        criteria=criteria or None,
        outcome=outcome.value,
        status_code=status_code,
        request_id=get_request_id(),
        **ctx.as_columns(),
    )


async def record_read(
    db: AsyncSession,
    entity_type: str,
    *,
    entity_id: uuid.UUID | None = None,
    entity_ids=None,
    count: int | None = None,
    criteria: dict | None = None,
    action: AuditAction = AuditAction.READ,
) -> None:
    """Record a PHI read on the request's own session, and commit it.

    Committed here rather than left pending because a read request has nothing
    else to commit — without this the row would be discarded when the session
    closes. expire_on_commit is False on the sessionmaker, so objects the route
    is about to serialise stay usable afterwards.
    """
    db.add(build_entry(
        action, entity_type,
        entity_id=entity_id, entity_ids=entity_ids, count=count, criteria=criteria,
    ))
    await db.commit()


async def record_export(
    db: AsyncSession,
    entity_type: str,
    *,
    count: int,
    criteria: dict | None = None,
    entity_ids=None,
) -> None:
    await record_read(
        db, entity_type,
        entity_ids=entity_ids, count=count, criteria=criteria,
        action=AuditAction.EXPORT,
    )


async def record_event(
    action: AuditAction,
    entity_type: str,
    *,
    entity_id: uuid.UUID | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    status_code: int | None = None,
    criteria: dict | None = None,
    context: AuditContext | None = None,
) -> None:
    """Record on a FRESH session, for events with no usable request session.

    Denied attempts are raised as exceptions, and by the time a handler sees
    one the request's session generator has already closed. A refusal must
    still be recorded, so it gets its own transaction.
    """
    async with AsyncSessionLocal() as db:
        db.add(build_entry(
            action, entity_type,
            entity_id=entity_id, outcome=outcome, status_code=status_code,
            criteria=criteria, context=context,
        ))
        await db.commit()


async def record_denied_on(
    db: AsyncSession,
    entity_type: str,
    *,
    entity_id: uuid.UUID | None = None,
    status_code: int = 404,
) -> None:
    """A refusal recorded on the request's own session, before it raises.

    Exists for the ownership checks. A therapist asking for a client outside
    their caseload gets a 404 — deliberately, so the response does not confirm
    the record exists — but a 404 is indistinguishable from "no such id" to the
    exception handler, which therefore cannot log it. Recording it here is what
    makes "someone went looking outside their caseload" visible, and that is
    the single most useful insider signal the log can carry.
    """
    db.add(build_entry(
        AuditAction.ACCESS_DENIED, entity_type,
        entity_id=entity_id, outcome=AuditOutcome.DENIED, status_code=status_code,
    ))
    await db.commit()


async def record_denied(
    entity_type: str,
    status_code: int,
    *,
    context: AuditContext | None = None,
) -> None:
    await record_event(
        AuditAction.ACCESS_DENIED, entity_type,
        outcome=AuditOutcome.DENIED, status_code=status_code, context=context,
    )


async def record_login(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    *,
    success: bool,
    email_domain: str | None = None,
) -> None:
    """A login outcome.

    NOTE the email address is not stored — only its domain, and only on
    failure. The address behind a failed attempt is the one field that would
    make this table useful to whoever reached it, while the domain is enough to
    tell "someone is spraying our staff" apart from "one person mistyped".
    """
    db.add(build_entry(
        AuditAction.LOGIN if success else AuditAction.LOGIN_FAILED,
        "user",
        entity_id=user_id,
        outcome=AuditOutcome.SUCCESS if success else AuditOutcome.DENIED,
        criteria={"email_domain": email_domain} if email_domain else None,
    ))
    await db.commit()


async def record_logout(db: AsyncSession, user_id: uuid.UUID | None) -> None:
    """A logout.

    /api/auth/logout deliberately carries no get_current_user dependency — it
    must still clear the cookie for an expired or revoked token rather than
    answering 401 — so there is no authenticated actor in the context. The id
    is recovered from the token being revoked and filled in as the actor, since
    "somebody logged out" without saying who is not worth a row.
    """
    ctx = current_context()
    if ctx.actor_user_id is None:
        ctx.actor_user_id = user_id
    db.add(build_entry(AuditAction.LOGOUT, "user", entity_id=user_id))
    await db.commit()


# ── retention ─────────────────────────────────────────────────────────────

async def purge_expired(db: AsyncSession, retention_days: int) -> int:
    """Delete audit rows past the retention window. The ONLY deleting path.

    This is the single documented exception to append-only (item 3.7), and it
    is constrained three ways so it cannot become "tidy away the inconvenient
    entries": it selects by AGE alone and never by actor, entity or outcome; it
    takes the window as an argument rather than from a request; and it writes a
    PURGE entry recording exactly what it removed, so the deletion is itself in
    the trail.

    NOTE this enforces only the retention CEILING. Trimming Postgres earlier by
    archiving cold rows to object storage is a storage optimisation and is NOT
    implemented — see the audit report.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    removed = result.rowcount or 0

    if removed:
        db.add(build_entry(
            AuditAction.PURGE, "audit_log",
            count=removed,
            criteria={"cutoff": cutoff.isoformat(), "retention_days": retention_days},
            context=AuditContext(
                actor_label="system:retention", actor_role="system",
                route="system:retention",
            ),
        ))
    await db.commit()
    if removed:
        logger.info("audit retention: removed %d rows older than %s", removed, cutoff)
    return removed


async def count_all(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(AuditLog))).scalar_one()
