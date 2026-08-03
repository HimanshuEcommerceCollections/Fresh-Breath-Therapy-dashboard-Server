from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.revoked_token import RevokedToken
from app.services.jwt_service import decode_token_claims


async def is_token_revoked(db: AsyncSession, jti: str) -> bool:
    result = await db.execute(select(RevokedToken.jti).where(RevokedToken.jti == jti))
    return result.scalar_one_or_none() is not None


async def revoke_token(db: AsyncSession, token: str) -> None:
    """Best-effort: an already-expired or malformed token has nothing worth
    revoking (it either can't be used again or was never valid), so this
    silently no-ops rather than failing logout over it."""
    claims = decode_token_claims(token)
    if claims is None:
        return

    jti = claims.get("jti")
    exp = claims.get("exp")
    if not jti or exp is None:
        return

    stmt = (
        pg_insert(RevokedToken)
        .values(jti=jti, expires_at=datetime.fromtimestamp(exp, tz=timezone.utc))
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    await db.execute(stmt)
    await db.commit()
