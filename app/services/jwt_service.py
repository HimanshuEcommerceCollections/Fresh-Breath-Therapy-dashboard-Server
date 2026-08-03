from datetime import datetime, timedelta, timezone
import uuid
from jose import jwt, JWTError
from app.config import settings


def create_access_token(user_id: uuid.UUID) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    # jti gives every issued token a unique identity so logout can revoke
    # this exact token server-side (see token_revocation_service) instead of
    # only clearing the cookie client-side — a copied/stolen token would
    # otherwise stay valid until it naturally expires.
    payload = {"sub": str(user_id), "exp": expire, "jti": str(uuid.uuid4())}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token_claims(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None


def decode_access_token(token: str) -> uuid.UUID | None:
    payload = decode_token_claims(token)
    if payload is None:
        return None
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None
