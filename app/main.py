import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

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
    # Bring the schema up to date before anything serves a request.
    #
    # This belongs in the start command (`alembic upgrade head && uvicorn ...`),
    # and the Procfile says so — but Railway's railpack builder reads only the
    # bare `uvicorn` invocation out of it and drops the chained migration step.
    # The result is an app whose models declare columns the database doesn't
    # have, which fails as a 500 on every endpoint touching those tables rather
    # than as anything that looks like a deployment problem.
    #
    # Running it here makes the deployment self-sufficient: whatever command the
    # platform decides to run, the schema is correct before the first request.
    # Alembic takes its own advisory lock, so a second replica starting at the
    # same time waits rather than racing.
    #
    # Set RUN_MIGRATIONS_ON_STARTUP=0 to disable (e.g. if you move to running
    # migrations as a separate release step).
    if os.getenv("RUN_MIGRATIONS_ON_STARTUP", "1") != "0":
        try:
            from alembic import command
            from alembic.config import Config

            cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
            # `script_location` in alembic.ini is relative ("alembic"), which
            # resolves against the process working directory — not the config
            # file. That is fine when alembic is invoked from the repo root and
            # silently wrong when the platform starts the app from anywhere
            # else, so pin it absolutely.
            cfg.set_main_option(
                "script_location",
                str(Path(__file__).resolve().parent.parent / "alembic"),
            )
            print("  [startup] applying database migrations ...")
            command.upgrade(cfg, "head")
            print("  [startup] migrations up to date")
        except Exception as e:
            # Loud, but not fatal: a healthy container that reports the problem
            # is more debuggable than one that dies before writing a log line.
            print(f"  [startup] MIGRATION FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

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
        print(f"  [startup] seed admin skipped: {type(e).__name__}: {e}")
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
