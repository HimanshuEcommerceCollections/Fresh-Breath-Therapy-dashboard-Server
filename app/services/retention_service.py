"""Retention enforcement for the tables that accumulated PHI indefinitely.

Audit item 5.7. Deletion in this application is otherwise a hard delete and
intentional; the problem was never the delete path, it was four tables that
nothing ever removed anything from:

  * import_rows        — every cell of every spreadsheet ever imported,
                         verbatim, kept forever. The rawest PHI in the system.
  * idempotency_keys   — FULL SERIALISED API RESPONSES, i.e. complete client
                         records with name, email and phone, as JSONB. In effect
                         a second copy of the client database, unaudited and
                         unpruned.
  * otp_codes          — one row per login attempt, kept after use.
  * revoked_tokens     — one row per logout, kept after the token it names has
                         itself expired and become unusable.
  * notifications      — bodies naming the client or lead they concern, kept
                         indefinitely after being read.

All four carried a TODO(retention) comment and none had a mechanism to hang a
policy on, which is exactly how a retention requirement goes quietly unmet.

TWO DIFFERENT TREATMENTS, deliberately:

  * import_rows is REDACTED IN PLACE, not deleted. The row number, the verdict
    and the id of what it produced are the audit trail of an import — "150 rows
    in, 148 imported" has to stay explainable, and a rollback reads entity_id.
    So the PHI-bearing columns are nulled and the bookkeeping survives.
  * the other three are DELETED outright. Nothing downstream reads them once
    they are spent, and keeping a spent OTP or a revocation record for a token
    that has already expired protects nobody.

Every pass writes an audit entry with its counts, so the deletion is itself in
the trail — same rule as the audit log's own purge.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.audit_log import AuditAction
from app.models.idempotency_key import IdempotencyKey
from app.models.import_batch import ImportBatch, ImportRow
from app.models.notification import Notification
from app.models.otp_code import OtpCode
from app.models.revoked_token import RevokedToken
from app.services.audit_context import AuditContext
from app.services.audit_service import build_entry

logger = logging.getLogger(__name__)

# Batch states after which a row's raw contents are no longer needed. A batch
# still in mapping or preview is mid-workflow and must keep everything.
_SETTLED_STATUSES = ("committed", "rolled_back", "failed")

_RETENTION_CONTEXT = AuditContext(
    actor_label="system:retention", actor_role="system", route="system:retention",
)


def _audit(db: AsyncSession, entity_type: str, count: int, criteria: dict) -> None:
    db.add(build_entry(
        AuditAction.PURGE, entity_type,
        count=count, criteria=criteria, context=_RETENTION_CONTEXT,
    ))


async def redact_settled_import_rows(db: AsyncSession, retention_days: int) -> int:
    """Null the PHI columns of import rows whose batch settled long enough ago.

    Redacted rather than deleted: row_number, status, errors and entity_id are
    what make an import explainable afterwards and what a rollback walks. Only
    the columns holding the spreadsheet's actual contents go.

    `errors` is left alone because it no longer contains cell values — the
    messages were rewritten to carry the reason and the column name only.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    settled_batches = (
        select(ImportBatch.id)
        .where(ImportBatch.status.in_(_SETTLED_STATUSES))
        .where(func.coalesce(ImportBatch.committed_at, ImportBatch.updated_at) < cutoff)
    )
    result = await db.execute(
        update(ImportRow)
        .where(ImportRow.batch_id.in_(settled_batches))
        # Only rows that still hold something, so a second pass is a no-op
        # rather than rewriting the same rows every night.
        .where(ImportRow.raw_payload != {})
        .values(
            raw_payload={},
            # null() rather than Python None. On a JSON/JSONB column SQLAlchemy
            # renders None as the JSON VALUE null, not SQL NULL — so the column
            # would hold 'null'::jsonb and `IS NULL` would never match it.
            # Harmless for reads (both deserialise to None) but wrong, and it
            # makes "have these been redacted yet" unanswerable in SQL.
            normalized_payload=null(),
            overrides=null(),
            diff=null(),
        )
    )
    redacted = result.rowcount or 0
    if redacted:
        _audit(db, "import_row", redacted, {
            "action": "redacted_in_place",
            "cutoff": cutoff.isoformat(),
            "retention_days": retention_days,
        })
    return redacted


async def purge_idempotency_keys(db: AsyncSession, retention_hours: int) -> int:
    """Delete stored API responses past their replay window.

    These exist to make a retried request safe, which is a question answered
    within seconds — Stripe keeps theirs 24 hours. Everything after that is a
    copy of a client record kept for no reason.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    result = await db.execute(
        delete(IdempotencyKey).where(IdempotencyKey.created_at < cutoff)
    )
    removed = result.rowcount or 0
    if removed:
        _audit(db, "idempotency_key", removed, {
            "cutoff": cutoff.isoformat(), "retention_hours": retention_hours,
        })
    return removed


async def purge_spent_otp_codes(db: AsyncSession) -> int:
    """Delete OTP rows that can no longer be used.

    Expired or consumed, plus a day's grace so a support question about a login
    that happened this morning can still be answered.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    result = await db.execute(delete(OtpCode).where(OtpCode.expires_at < cutoff))
    removed = result.rowcount or 0
    if removed:
        _audit(db, "otp_code", removed, {"cutoff": cutoff.isoformat()})
    return removed


async def purge_expired_revoked_tokens(db: AsyncSession) -> int:
    """Delete revocation records for tokens that have themselves expired.

    Once a token's own exp has passed it is refused on that ground alone, so
    remembering that it was also revoked serves nothing. The login event stays
    in the audit log; this table is only a fast rejection list.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    removed = result.rowcount or 0
    if removed:
        _audit(db, "revoked_token", removed, {"cutoff": now.isoformat()})
    return removed


async def purge_old_notifications(db: AsyncSession, retention_days: int) -> int:
    """Delete READ notifications past the retention window.

    Their bodies name the client or lead they concern — see scheduler_service
    and webhooks — which is what makes them useful and is now only visible to
    someone entitled to read that name, since the therapist_id IS NULL loophole
    was closed. A read reminder from four months ago is PHI kept for nothing.

    Unread rows are left alone at any age: an unread notification is
    outstanding work, and deleting it silently drops the reminder rather than
    the data.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = await db.execute(
        delete(Notification)
        .where(Notification.is_read.is_(True))
        .where(Notification.created_at < cutoff)
    )
    removed = result.rowcount or 0
    if removed:
        _audit(db, "notification", removed, {
            "cutoff": cutoff.isoformat(), "retention_days": retention_days,
            "scope": "read_only",
        })
    return removed


async def run_retention_sweep(db: AsyncSession) -> dict[str, int]:
    """Every retention rule, in one transaction.

    One commit at the end so the sweep and the audit entries describing it land
    together — a sweep whose record went missing would be worse than one that
    had not run.
    """
    counts = {
        "import_rows_redacted": await redact_settled_import_rows(
            db, settings.IMPORT_ROW_RETENTION_DAYS
        ),
        "idempotency_keys_removed": await purge_idempotency_keys(
            db, settings.IDEMPOTENCY_KEY_RETENTION_HOURS
        ),
        "otp_codes_removed": await purge_spent_otp_codes(db),
        "revoked_tokens_removed": await purge_expired_revoked_tokens(db),
        "notifications_removed": await purge_old_notifications(
            db, settings.NOTIFICATION_RETENTION_DAYS
        ),
    }
    await db.commit()
    if any(counts.values()):
        logger.info("retention sweep: %s", counts)
    return counts
