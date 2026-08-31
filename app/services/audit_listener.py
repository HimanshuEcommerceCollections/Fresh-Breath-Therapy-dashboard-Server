"""Automatic audit capture for every write, via the ORM.

WHY A FLUSH HOOK RATHER THAN CALLS IN ROUTES.

Item 3.9 asks for coverage that is structural rather than remembered. Audit
calls sprinkled per route are wrong not because they are ugly but because they
are incomplete the first time someone adds an endpoint without reading this
file — and a log with unknown gaps is worse than no log, since it will be
trusted. SQLAlchemy already knows the exact moment a row is inserted, changed
or deleted; it has to, that is how it writes the SQL. Hooking that moment means
an endpoint written next year is covered on the day it is written, by someone
who never heard of this module.

WHY before_flush AND NOT after_flush.

Two reasons, both load-bearing:

  * Attribute history is intact here. get_history() still reports the old
    value, which is the whole of item 3.4 — being able to prove a payment
    amount went from one number to another.
  * Objects added during before_flush are picked up by the same flush, so the
    audit rows land in the SAME TRANSACTION as the data they describe. That is
    what makes item 3.10 free rather than a policy: either both commit or
    neither does. There is no "the payment was updated but its audit record
    vanished" state to design around, and no try/except swallowing a failure.
"""
import logging
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.audit_log import AuditAction, AuditLog, AuditOutcome
from app.services.audit_context import current_context
from app.services.audit_registry import (
    NEVER_RECORDED, entity_type_for, is_identifying,
)
from app.middleware.request_id import get_request_id

logger = logging.getLogger(__name__)

REDACTED = {"redacted": True}

# Never worth a line in a diff. `id` is already the entity_id column, and the
# timestamps are bookkeeping: created_at is still null at flush time on an
# insert (it has a server default), and updated_at changes on literally every
# update, which would bury the one field that actually changed.
SKIPPED_FIELDS = frozenset({"id", "created_at", "updated_at"})


def _jsonable(value):
    """JSONB-safe rendering of a column value.

    Deliberately conservative: anything unrecognised becomes a type name
    rather than str(value), because str() of an unexpected object is exactly
    how a nested ORM instance — and the patient data hanging off it — would end
    up serialised into this table.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (uuid.UUID, Decimal, date, datetime, time)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return f"<{type(value).__name__}>"


def _column_keys(instance) -> list[str]:
    """Column-backed attributes only — relationships are skipped.

    Walking relationships would pull related rows into the diff and with them
    the very values this must not record.
    """
    return [c.key for c in inspect(type(instance)).mapper.column_attrs]


def _field_entry(entity_type: str, field: str, old=None, new=None, include_old=True,
                 include_new=True):
    if is_identifying(entity_type, field):
        return REDACTED
    entry = {}
    if include_old:
        entry["old"] = _jsonable(old)
    if include_new:
        entry["new"] = _jsonable(new)
    return entry


def _snapshot(instance, entity_type: str, *, as_old: bool) -> dict:
    """Every column of a row, for a create or a delete.

    A delete needs this most: once the row is gone there is nothing left to
    look up, so "what was deleted" has to be captured at the moment it happens.
    """
    out = {}
    for field in _column_keys(instance):
        if field in NEVER_RECORDED or field in SKIPPED_FIELDS:
            continue
        value = getattr(instance, field, None)
        out[field] = _field_entry(
            entity_type, field,
            old=value if as_old else None,
            new=None if as_old else value,
            include_old=as_old,
            include_new=not as_old,
        )
    return out


def _changes(instance, entity_type: str) -> dict:
    """Only the columns this flush actually changes, with old and new."""
    state = inspect(instance)
    out = {}
    for field in _column_keys(instance):
        if field in NEVER_RECORDED or field in SKIPPED_FIELDS:
            continue
        history = state.attrs[field].history
        if not history.has_changes():
            continue
        old = history.deleted[0] if history.deleted else None
        new = history.added[0] if history.added else None
        # SQLAlchemy reports a change when a value is reassigned even if it is
        # equal; recording those would bury the real edits in noise.
        if old == new:
            continue
        out[field] = _field_entry(entity_type, field, old=old, new=new)
    return out


def _row(action: AuditAction, entity_type: str, entity_id, changed_fields) -> AuditLog:
    ctx = current_context()
    return AuditLog(
        id=uuid.uuid4(),
        action=action.value,
        entity_type=entity_type,
        entity_id=entity_id,
        changed_fields=changed_fields or None,
        outcome=AuditOutcome.SUCCESS.value,
        request_id=get_request_id(),
        **ctx.as_columns(),
    )


@event.listens_for(Session, "before_flush")
def _capture_writes(session: Session, flush_context, instances) -> None:
    """Turn this flush's pending changes into audit rows."""
    pending: list[AuditLog] = []

    # Collected first, added afterwards: session.new is an identity set and
    # adding to it while iterating would mutate it mid-loop.
    for instance in session.new:
        entity_type = entity_type_for(instance)
        if entity_type is None:
            continue
        pending.append(_row(
            AuditAction.CREATE, entity_type, getattr(instance, "id", None),
            _snapshot(instance, entity_type, as_old=False),
        ))

    for instance in session.dirty:
        entity_type = entity_type_for(instance)
        if entity_type is None:
            continue
        if not session.is_modified(instance, include_collections=False):
            continue
        changes = _changes(instance, entity_type)
        if not changes:
            continue  # touched but nothing actually different
        pending.append(_row(
            AuditAction.UPDATE, entity_type, getattr(instance, "id", None), changes,
        ))

    for instance in session.deleted:
        entity_type = entity_type_for(instance)
        if entity_type is None:
            continue
        pending.append(_row(
            AuditAction.DELETE, entity_type, getattr(instance, "id", None),
            _snapshot(instance, entity_type, as_old=True),
        ))

    # AuditLog is absent from the registry, so these additions produce no
    # further entries and this cannot recurse.
    for row in pending:
        session.add(row)


def register_audit_listener() -> None:
    """Importing this module is what registers the hook; this exists so the
    import in main.py reads as deliberate rather than looking unused."""
    logger.debug("audit write listener registered")
