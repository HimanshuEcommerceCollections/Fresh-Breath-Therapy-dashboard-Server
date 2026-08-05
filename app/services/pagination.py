import base64
import json
import uuid
from datetime import datetime
from typing import Generic, TypeVar
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.sql import Select

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    payload = {"created_at": created_at.isoformat(), "id": str(row_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["created_at"]), uuid.UUID(payload["id"])
    except (ValueError, KeyError, TypeError):
        return None


def apply_keyset_pagination(query: Select, model, cursor: str | None, limit: int) -> Select:
    """Keyset (cursor) pagination, ordered by (created_at, id) DESC — the
    production-grade alternative to OFFSET/LIMIT for tables that grow
    unbounded. An OFFSET has to scan and discard every skipped row, so it
    gets linearly slower the deeper a user pages/scrolls; rows inserted
    while someone is mid-scroll can also shift page boundaries and cause
    duplicated or skipped items. Keyset pagination is a single indexed range
    condition regardless of depth, and is stable under concurrent inserts.

    Always requests one row more than `limit` so `has_more` is known without
    a separate COUNT(*) query — see paginate_rows.
    """
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is not None:
            cursor_created_at, cursor_id = decoded
            query = query.where(
                or_(
                    model.created_at < cursor_created_at,
                    and_(model.created_at == cursor_created_at, model.id < cursor_id),
                )
            )
    return query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1)


def paginate_rows(rows: list, limit: int) -> tuple[list, str | None, bool]:
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    next_cursor = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return page_rows, next_cursor, has_more
