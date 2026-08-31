from datetime import datetime, timedelta, timezone
import uuid
from jose import jwt, JWTError
from app.config import settings


def create_access_token(
    user_id: uuid.UUID, session_started_at: datetime | None = None
) -> str:
    """Mint an access token.

    `session_started_at` is carried forward unchanged every time a token is
    re-issued for an active session, which is what lets the sliding window have
    an absolute ceiling: the token's own exp is the IDLE limit, and `sst` is
    when the human actually signed in.

    Passing None means "this is a fresh sign-in" and stamps now.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    # jti gives every issued token a unique identity so logout can revoke
    # this exact token server-side (see token_revocation_service) instead of
    # only clearing the cookie client-side — a copied/stolen token would
    # otherwise stay valid until it naturally expires.
    # `sst` is converted by hand. python-jose only coerces datetimes for the
    # registered claims exp/iat/nbf; a custom claim reaches json.dumps as-is and
    # raises "Object of type datetime is not JSON serializable".
    session_start = session_started_at or now
    payload = {
        "sub": str(user_id),
        "exp": expire,
        "iat": now,
        "sst": int(session_start.timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def claim_as_datetime(claims: dict, key: str) -> datetime | None:
    """A numeric-date claim as an aware datetime, or None if absent/unusable.

    Tolerant on purpose: tokens minted before these claims existed are still in
    browsers, and they must degrade to "no sliding, no absolute cap" rather
    than logging everyone out on deploy. They expire within the idle window
    anyway.
    """
    raw = claims.get(key)
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


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
