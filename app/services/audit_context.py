"""Ambient who/where for the current request.

Audit rows get written from inside SQLAlchemy flush events and from service
functions several calls below any route. None of them can see the Request, and
threading an actor argument through every signature to reach them is exactly
the discipline that decays — one missed hand-off and a record is written with
no actor, silently.

So the actor lives in a ContextVar, set once per request and read wherever it
is needed. Same mechanism as request_id, and for the same reason.

The object is deliberately MUTABLE and installed early. Middleware knows the
IP, user agent and route before authentication has happened; the identity only
becomes known later, inside get_current_user. Both write into the same object
rather than replacing it, so a value set by the dependency is visible to code
that captured the context earlier in the request.
"""
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

from app.models.audit_log import MAX_USER_AGENT_LENGTH


@dataclass
class AuditContext:
    """Who is acting, and from where. Never holds patient data."""

    source_ip: str | None = None
    user_agent: str | None = None
    route: str | None = None
    actor_user_id: uuid.UUID | None = None
    actor_role: str | None = None
    actor_label: str | None = None

    def as_columns(self) -> dict:
        """The subset of an audit row this context supplies."""
        return {
            "actor_user_id": self.actor_user_id,
            "actor_role": self.actor_role,
            "actor_label": self.actor_label,
            "source_ip": self.source_ip,
            "user_agent": self.user_agent,
            "route": self.route,
        }


_audit_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


def install_context(ctx: AuditContext):
    """Put a context in place. Returns the token needed to reset it."""
    return _audit_context.set(ctx)


def reset_context(token) -> None:
    _audit_context.reset(token)


def current_context() -> AuditContext:
    """The ambient context, or an empty one.

    Never raises and never returns None. An audit row with no actor is far
    better than an exception that loses the record entirely — and an empty
    actor is itself information: it means something wrote outside a request
    without declaring a system identity, which is a bug worth seeing in the
    data.
    """
    return _audit_context.get() or AuditContext()


def set_actor(
    user_id: uuid.UUID | None,
    role: str | None,
    label: str | None = None,
) -> None:
    """Attach the authenticated identity to the in-flight context.

    Called from get_current_user, i.e. the single point every authenticated
    request passes through, so no route has to remember to do it.
    """
    ctx = _audit_context.get()
    if ctx is None:
        ctx = AuditContext()
        _audit_context.set(ctx)
    ctx.actor_user_id = user_id
    ctx.actor_role = role
    ctx.actor_label = label


def system_context(label: str) -> AuditContext:
    """A named non-human actor, e.g. "system:scheduler".

    The APScheduler jobs read every open follow-up and its client record on a
    15-minute timer, entirely outside the request/auth layer (audit item 7.3).
    Without an explicit identity that is the most regular PHI access in the
    system and the least accountable, so it gets a name rather than a null.
    """
    return AuditContext(actor_label=label, actor_role="system", route=label)


def truncate_user_agent(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_USER_AGENT_LENGTH]
