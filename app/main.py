from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import AsyncSessionLocal, log_connection_mode
from app.middleware.csrf import CsrfProtectionMiddleware
from app.startup import ensure_auth_bootstrap
from app.routers import (
    auth, locations, therapists, leads, clients, follow_up,
    organization, roles, packages, feature_flags,
    payments, enrollments, reports, oauth_google, uploads, sessions, dashboard,
    pto, notifications, client_messages, internal, exports, webhooks, imports
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
_docs_enabled = settings.is_development

app = FastAPI(
    title="FBT Dashboard API",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

allowed_origins = [origin.strip() for origin in settings.ALLOWED_ORIGINS.split(",") if origin.strip()]

# ORDER MATTERS. Starlette runs the most recently added middleware outermost,
# so CORS must be added LAST to end up wrapping the CSRF check. That way a
# rejection from CsrfProtectionMiddleware still gets CORS headers applied on
# the way out, and a legitimate origin that somehow trips it sees a readable
# 403 rather than an opaque browser network error.
#
# Both read the SAME allowed_origins list, deliberately: CORS decides who may
# read a response, the CSRF check decides who may cause a write, and they must
# never disagree about which origins are ours.
app.add_middleware(CsrfProtectionMiddleware, allowed_origins=allowed_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.on_event("startup")
async def _start_scheduler():
    start_scheduler()

@app.on_event("startup")
async def _log_db_mode():
    log_connection_mode()

@app.on_event("startup")
async def on_startup():
    async with AsyncSessionLocal() as db:
        await ensure_auth_bootstrap(db)

@app.get("/health")
async def health_check():
    return {"status": "ok"}