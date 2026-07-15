from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import (
    auth, locations, therapists, leads, clients, follow_up,
    organization, roles, packages, feature_flags, integrations,
)

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

@app.get("/health")
async def health_check():
    return {"status": "ok"}