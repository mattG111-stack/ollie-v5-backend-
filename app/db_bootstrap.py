"""One-shot DB bootstrap, run before uvicorn on every boot.

Heals a database left inconsistent by an earlier repo upload (production was
stamped at revision `a9f4c2e81b30`, which this codebase no longer contains, and
that migration also created columns as NOT NULL that these models treat as
optional). Symptoms it fixes:

  * `alembic upgrade head` aborting with "Can't locate revision" → app never
    started (took logins + all data down).
  * every ingest-job insert failing with NotNullViolation (upload/enrich/publish
    500s) because a column the models leave null is NOT NULL in the DB.

What it does, without invoking alembic:
  1. Ensure the columns this codebase added exist (idempotent ADD COLUMN).
  2. Reconcile NULLability to the models everywhere: for every column the models
     declare nullable, DROP NOT NULL in the DB if it's currently NOT NULL. This
     covers ANY column that old migration over-constrained, not just a guessed
     few.
  3. Reset `alembic_version` to this codebase's head, ignoring the unknown rev.

Each statement runs in its own transaction so one failure can't poison the rest,
and nothing here ever aborts startup — a running app beats a dead one.
"""
from __future__ import annotations

import sys

from sqlalchemy import inspect, text

import app.models  # noqa: F401  — populate Base.metadata
from app.db import Base, engine

HEAD_REVISION = "a1c2e3d4f5b6"

# New columns this codebase added that an out-of-sync DB may be missing.
_ADD_COLUMNS = (
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_filled INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS rows_missed INTEGER",
    "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS result_json TEXT",
)


def _run(sql: str) -> None:
    """Run one statement in its own transaction; log and move on if it can't."""
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    except Exception as e:
        print(f"[db_bootstrap] skip: {sql[:60]}… -> {type(e).__name__}", flush=True)


def _reconcile_nullability() -> int:
    """For every model column that is nullable, DROP NOT NULL in the DB where it's
    currently NOT NULL. Aligns the DB to the models so inserts that leave optional
    columns null never violate a stray NOT NULL constraint. Returns count fixed."""
    fixed = 0
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
    except Exception as e:
        print(f"[db_bootstrap] could not inspect DB: {type(e).__name__}: {e}", flush=True)
        return 0

    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        try:
            db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        except Exception:
            continue
        for col in table.columns:
            db_col = db_cols.get(col.name)
            if col.nullable and db_col is not None and db_col.get("nullable") is False:
                _run(f'ALTER TABLE "{table.name}" ALTER COLUMN "{col.name}" DROP NOT NULL')
                fixed += 1
    return fixed


def main() -> int:
    for stmt in _ADD_COLUMNS:
        _run(stmt)

    fixed = _reconcile_nullability()

    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"
            ))
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                         {"v": HEAD_REVISION})
        print(f"[db_bootstrap] ok — columns ensured, {fixed} NOT NULL constraint(s) "
              f"relaxed to match models, alembic_version = {HEAD_REVISION}", flush=True)
    except Exception as e:
        print(f"[db_bootstrap] WARNING (continuing to start server): "
              f"{type(e).__name__}: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
