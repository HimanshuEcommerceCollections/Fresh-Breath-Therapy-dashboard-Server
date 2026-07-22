import uuid
import functools
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_key import IdempotencyKey

IDEMPOTENCY_HEADER = "Idempotency-Key"

# TODO(retention): idempotency_keys rows only need to live ~24h (Stripe keeps
# theirs for 24h). No cleanup job exists yet — when a scheduled-job mechanism
# is added, run: DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours'


def idempotent(response_model=None, status_code: int = 200):
    """Opt-in Idempotency-Key support for a route (Stripe-style).

    Apply BELOW the @router.post(...) decorator. The route's signature must
    include `request: Request` and `db: AsyncSession = Depends(get_db)`.

    - No Idempotency-Key header → the route runs normally, nothing is stored.
    - Header present, (key, endpoint) already stored → the route body does NOT
      run; the stored status/body is replayed verbatim.
    - Header present, not stored → the route runs, then its serialized response
      is stored under (key, endpoint) before returning.
    - Two simultaneous requests with the same key: the second INSERT hits the
      unique constraint → 409 "Duplicate request already in progress".

    `response_model`/`status_code` must mirror the ones on @router.post — they
    are what FastAPI would use to serialize, so the stored body matches what
    the client actually received.
    """
    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(*args, **kwargs):
            request: Request = kwargs["request"]
            db: AsyncSession = kwargs["db"]

            key = request.headers.get(IDEMPOTENCY_HEADER)
            if key is None:
                return await handler(*args, **kwargs)

            endpoint = f"{request.method} {request.url.path}"

            result = await db.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.key == key,
                    IdempotencyKey.endpoint == endpoint,
                )
            )
            stored = result.scalar_one_or_none()
            if stored is not None:
                return JSONResponse(
                    status_code=stored.response_status,
                    content=stored.response_body,
                )

            response_value = await handler(*args, **kwargs)

            if response_model is not None:
                body = jsonable_encoder(response_model.model_validate(response_value))
            else:
                body = jsonable_encoder(response_value)

            current_user = kwargs.get("current_user")
            db.add(IdempotencyKey(
                id=uuid.uuid4(),
                key=key,
                user_id=current_user.id if current_user is not None else None,
                endpoint=endpoint,
                response_status=status_code,
                response_body=body,
            ))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Duplicate request already in progress",
                )

            return response_value

        return wrapper
    return decorator
