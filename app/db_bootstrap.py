"""One-shot DB bootstrap, run before uvicorn on every boot.

Makes startup resilient to a migration-history mismatch. Production's
`alembic_version` can point at a revision this codebase no longer contains
(e.g. `a9f4c2e81b30`, left behind by an earlier repo upload). `alembic upgrade
head` then aborts with "Can't locate revision", and because the Procfile chained
it with `&&`, uvicorn never started — the whole app (logins included) went down.

This bypasses alembic's revision walk entirely:
  1. Ensure the columns the current models need exist (idempotent ADD COLUMN).
  2. Reset `alembic_version` to THIS codebase's head, whatever (possibly unknown)
     revision it was on before — so alembic is consistent again.

It never raises: a bootstrap hiccup logs a warning and still lets the server
start. Safe to run on every deploy.
"""
from __future__ import annotations

import sys

from sqlalchemy import text

from app.db import engine

# The head revision in this codebase's alembic/versions. Keep in sync if you add
# migrations (or re-point it at your latest head).
HEAD_REVISION = "a1c2e3d4f5b6"

# Columns this codebase added that may be missing on an out-of-sync DB. Postgres
# ADD COLUMN IF NOT EXISTS makes each a no-op when the column is already there.
_ENSURE_COLUMNS = (
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_filled INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_missed INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS result_json TEXT",
)


def main() -> int:
    try:
        with engine.begin() as conn:
            for stmt in _ENSURE_COLUMNS:
                try:
                    conn.execute(text(stmt))
                except Exception as e:  # e.g. table missing on a fresh DB — alembic will make it
                    print(f"[db_bootstrap] skip: {stmt} -> {type(e).__name__}: {e}", flush=True)

            # Make alembic consistent: point it at this codebase's head, ignoring
            # any unknown revision it was stamped at before.
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                         {"v": HEAD_REVISION})
        print(f"[db_bootstrap] ok — columns ensured, alembic_version = {HEAD_REVISION}", flush=True)
    except Exception as e:
        # Never block startup on a bootstrap problem — a running app beats a dead one.
        print(f"[db_bootstrap] WARNING (continuing to start server): {type(e).__name__}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
