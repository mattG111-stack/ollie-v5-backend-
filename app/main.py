from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import SessionLocal
from .routers import (
    admin_metrics,
    admin_stages,
    admin_upload,
    assistant,
    auth,
    billing,
    dashboards,
    properties,
    release,
    wishlists,
)
from .security import ensure_seed_admin, require_active


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Seeding must never prevent the app from starting. If it throws — most
    # often because the migration hasn't run and `users` doesn't exist yet — an
    # unhandled exception here means uvicorn never binds, the healthcheck never
    # passes, and the platform kills the container before any log explaining
    # why is readable. Serving /health with a loud error is far more debuggable
    # than a replica that never comes up.
    db = SessionLocal()
    try:
        ensure_seed_admin(db)
    except Exception as e:
        print(
            f"  [startup] seed admin skipped: {type(e).__name__}: {e}\n"
            f"  [startup] this usually means migrations have not run. The start "
            f"command must be:\n"
            f"  [startup]   alembic upgrade head && uvicorn app.main:app "
            f"--host 0.0.0.0 --port $PORT"
        )
    finally:
        db.close()
    yield


app = FastAPI(
    title="Ollie Property Intelligence API",
    version="0.1.0",
    description="Backend for the Ollie property platform.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The product data itself is paywalled: no free access. These routers require an
# active subscription (or admin / admin-approved account) — an authenticated but
# not-yet-subscribed user gets 402 and the frontend routes them to onboarding.
PAYWALL = [Depends(require_active)]

app.include_router(auth.router)
app.include_router(auth.admin_router)
app.include_router(properties.router, dependencies=PAYWALL)
app.include_router(assistant.router, dependencies=PAYWALL)
app.include_router(properties.sold_router, dependencies=PAYWALL)
app.include_router(dashboards.router, dependencies=PAYWALL)
app.include_router(admin_upload.router)
app.include_router(admin_stages.router)
app.include_router(admin_metrics.router)
app.include_router(release.router)
app.include_router(billing.router)
app.include_router(wishlists.router, dependencies=PAYWALL)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "Ollie API", "docs": "/docs"}
