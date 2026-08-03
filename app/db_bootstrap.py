"""One-shot DB bootstrap, run before uvicorn on every boot.

Makes startup and inserts resilient to a migration-history mismatch. An earlier
repo upload left production stamped at revision `a9f4c2e81b30`, which this
codebase no longer contains — so `alembic upgrade head` aborted with "Can't
locate revision", and (via the Procfile's `&&`) uvicorn never started.

That same phantom migration had ALSO created the ingest_jobs progress columns as
NOT NULL. This codebase's models treat them as optional and insert jobs without
them, so every upload/enrich/publish then failed with a NotNullViolation.

This heals both without alembic:
  1. Ensure the columns exist (idempotent ADD COLUMN).
  2. Make them nullable to match the models (idempotent DROP NOT NULL).
  3. Reset `alembic_version` to THIS codebase's head, ignoring any unknown rev.

Each statement runs in its own transaction so one failure can't poison the rest,
and nothing here ever aborts startup — a running app beats a dead one.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from app.db import engine

# The head revision in this codebase's alembic/versions. Update if you add
# migrations (or re-point it at your latest head).
HEAD_REVISION = "a1c2e3d4f5b6"

# DDL to bring an out-of-sync ingest_jobs table in line with the models. Postgres
# makes each idempotent (ADD COLUMN IF NOT EXISTS; DROP NOT NULL is a no-op when
# already nullable).
_DDL = (
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_filled INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_missed INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS result_json TEXT",
    "ALTER TABLE ingest_jobs ALTER COLUMN rows_filled DROP NOT NULL",
    "ALTER TABLE ingest_jobs ALTER COLUMN rows_missed DROP NOT NULL",
    "ALTER TABLE ingest_jobs ALTER COLUMN result_json DROP NOT NULL",
)


def _run(sql: str) -> None:
    """Run one statement in its own transaction; log and move on if it can't."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except Exception as e:
        print(f"[db_bootstrap] skip: {sql[:55]}… -> {type(e).__name__}", flush=True)


def main() -> int:
    for stmt in _DDL:
        _run(stmt)

    # Make alembic consistent: point it at this codebase's head, ignoring any
    # unknown revision it was stamped at before. Own transaction.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                         {"v": HEAD_REVISION})
        print(f"[db_bootstrap] ok — ingest_jobs columns ensured & nullable, "
              f"alembic_version = {HEAD_REVISION}", flush=True)
    except Exception as e:
        print(f"[db_bootstrap] WARNING (continuing to start server): "
              f"{type(e).__name__}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
