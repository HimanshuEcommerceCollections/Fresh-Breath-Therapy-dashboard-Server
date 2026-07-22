from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.database import AsyncSessionLocal
from app.startup import ensure_auth_bootstrap
from app.routers import (
    auth, locations, therapists, leads, clients, follow_up,
    organization, roles, packages, feature_flags, integrations, 
    payments, reports, oauth_google, uploads, sessions, dashboard, 
    pto, notifications, client_messages
)
from app.services.scheduler_service import start_scheduler

app = FastAPI(title="FBT Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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
app.include_router(integrations.router)
app.include_router(payments.router)
app.include_router(reports.router)
app.include_router(oauth_google.router)
app.include_router(uploads.router)
app.include_router(sessions.router)
app.include_router(dashboard.router)
app.include_router(pto.router)
app.include_router(notifications.router)
app.include_router(client_messages.router)

@app.on_event("startup")
async def _start_scheduler():
    start_scheduler()

@app.on_event("startup")
async def on_startup():
    async with AsyncSessionLocal() as db:
        await ensure_auth_bootstrap(db)

@app.get("/health")
async def health_check():
    return {"status": "ok"}