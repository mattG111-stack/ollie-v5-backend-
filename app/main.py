from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import SessionLocal
from .routers import admin_metrics, admin_upload, assistant, auth, billing, dashboards, properties, release, wishlists
from .security import ensure_seed_admin, require_active


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = SessionLocal()
    try:
        ensure_seed_admin(db)
    finally:
        db.close()
    # Self-heal any staged batch still holding pre-guard CV-over valuations, in a
    # background daemon thread so a deploy fixes the stored numbers on its own —
    # no operator has to remember to click "Price". Backgrounded (never awaited)
    # so it can't delay startup or block the /health check, and it no-ops fast
    # when nothing is stale.
    import threading

    from .staged_stages import auto_reprice_stale_batches
    threading.Thread(target=auto_reprice_stale_batches, daemon=True).start()
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
