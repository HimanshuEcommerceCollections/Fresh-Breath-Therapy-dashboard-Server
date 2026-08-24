from dotenv import load_dotenv
load_dotenv()
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.config import settings
from app.database import AsyncSessionLocal, log_connection_mode
from app.middleware.audit_context import AuditContextMiddleware
from app.middleware.cache import NoStoreCacheMiddleware
from app.middleware.csrf import CsrfProtectionMiddleware
from app.middleware.errors import (
    http_exception_audit_handler, unhandled_exception_handler,
)
from app.middleware.headers import SecurityHeadersMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.startup import ensure_auth_bootstrap
# Imported for its side effect: this is what registers the before_flush hook
# that turns every ORM write into an audit record.
from app.services.audit_listener import register_audit_listener
from app.services.log_redaction import install_phi_log_redaction
from app.routers import (
    auth, locations, therapists, leads, clients, follow_up,
    organization, roles, packages, feature_flags,
    payments, enrollments, reports, oauth_google, uploads, sessions, dashboard,
    pto, notifications, client_messages, internal, exports, webhooks, imports,
    audit_logs,
)
from app.services.scheduler_service import start_scheduler

# ── interactive docs: development only ───────────────────────────────────
#
# All THREE of these have to be switched off together, and openapi_url is the
# one that matters. /docs and /redoc are just renderers; the actual disclosure
# is the schema document itself, which anonymously served up 135KB describing
# 76 paths, 100 operations and 124 schemas — 44 of which name PHI fields
# (LeadResponse alone publishes name, age, gender_or_pronoun, email, phone,
# message). That is a machine-readable map of exactly what patient data this
# system holds, handed to anyone who asks, before any authentication.
#
# It also published /api/asdv4nh45j-sdvvwe5-sd7cf8vw-dcsd/leads in full, so
# the webhook's unguessable-looking prefix protected nothing and
# LEAD_WEBHOOK_SECRET was doing all the work; and it enumerated the whole auth
# surface, including the endpoints most worth attacking.
#
# Passing None does not hide the route behind a 403 — it is never registered,
# so there is nothing there to probe.
@asynccontextmanager
async def lifespan(_: FastAPI):
    """Startup work, as a lifespan handler.

    Replaces three @app.on_event("startup") hooks. on_event still functions but
    is deprecated and warns on every boot — and a deprecation left in place is
    how a dependency bump eventually becomes an outage. Same three steps, same
    order, one handler.
    """
    log_connection_mode()
    async with AsyncSessionLocal() as db:
        await ensure_auth_bootstrap(db)
    start_scheduler()
    yield


_docs_enabled = settings.is_development

app = FastAPI(
    title="FBT Dashboard API",
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

allowed_origins = [origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()]

# ORDER MATTERS. Starlette runs the most recently added middleware outermost,
# so CORS must be registered AFTER the CSRF check in order to wrap it. That way
# a rejection from CsrfProtectionMiddleware still gets CORS headers applied on
# the way out, and a legitimate origin that somehow trips it sees a readable
# 403 rather than an opaque browser network error.
#
# Both read the SAME allowed_origins list, deliberately: CORS decides who may
# read a response, the CSRF check decides who may cause a write, and they must
# never disagree about which origins are ours.
app.add_middleware(CsrfProtectionMiddleware, allowed_origins=allowed_origins)

# WRAPS the CSRF check (registered after it, so it sits outside), because a
# cross-site write attempt is itself a denied attempt worth recording and the
# CSRF middleware needs a context to record it against.
# Registered BEFORE CORS so CORS ends up wrapping it. A 429 has to travel back
# out through CORSMiddleware or the browser reports an opaque network error
# instead of the status — and "slow down" is exactly the message the frontend
# should be able to read and show. The extra work per rejected request is a few
# header lookups, which is not what a flood costs.
app.add_middleware(RateLimitMiddleware)

app.add_middleware(AuditContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added LAST, so it is the OUTERMOST middleware and stamps every response on
# the way out — including ones produced by the middleware above it, such as a
# CSRF rejection or a CORS preflight. Nothing this API returns may be written
# to a disk cache that outlives the session; see app/middleware/cache.py.
app.add_middleware(NoStoreCacheMiddleware)

# Outermost, so it also stamps responses generated by the middleware above it.
# HSTS is withheld outside production so it is never even sent during local
# HTTP development; see app/middleware/headers.py for the rest.
app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=not settings.is_development)

# Outermost of all: every response, including those produced by the middleware
# below it, comes back with an X-Request-ID the caller can quote. Assigning the
# id here also means it exists before anything else can reject the request.
app.add_middleware(RequestIdMiddleware)

# Registered as the handler ServerErrorMiddleware uses, so it catches anything
# that escapes a route. Returns a request id and nothing else — see
# app/middleware/errors.py for why str(exc) must never reach the client.
app.add_exception_handler(Exception, unhandled_exception_handler)

# Records 401/403 as denied attempts (audit item 3.5) and then answers exactly
# as FastAPI would have; the client sees no difference.
app.add_exception_handler(StarletteHTTPException, http_exception_audit_handler)

register_audit_listener()

# Seatbelt, not a substitute for not logging PHI: the known offenders are
# gone, but nothing stops the next logger.info(f"client: {client}").
install_phi_log_redaction()

app.include_router(auth.router)
app.include_router(locations.router)
app.include_router(therapists.router)
app.include_router(leads.router)
app.include_router(clients.router)
app.include_router(follow_up.router)
app.include_router(organization.router)
app.include_router(roles.router)
app.include_router(packages.router)
app.include_router(feature_flags.router)
app.include_router(payments.router)
app.include_router(enrollments.router)
app.include_router(reports.router)
app.include_router(oauth_google.router)
app.include_router(uploads.router)
app.include_router(sessions.router)
app.include_router(dashboard.router)
app.include_router(pto.router)
app.include_router(notifications.router)
app.include_router(client_messages.router)
app.include_router(internal.router)
app.include_router(exports.router)
app.include_router(webhooks.router)
app.include_router(imports.router)
app.include_router(audit_logs.router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}