"""staged ingest: durable job progress + result column

Adds rows_filled / rows_missed (durable CoreLogic enrich counters), and
result_json (structured stage results — e.g. the publish result dict, which was
previously serialised into the varchar(64) `stage` label and raised
StringDataRightTruncation).

Idempotent: only adds columns that don't already exist. This matters because the
same columns may have been added manually (ALTER TABLE ... ADD COLUMN IF NOT
EXISTS) before this migration ran — adding an existing column would abort the
migration, and with the Procfile's `alembic upgrade head && uvicorn`, a failed
migration stops the whole app (including login) from starting.

Revision ID: a1c2e3d4f5b6
Revises: f2a3b4c5d6e7
Create Date: 2026-08-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c2e3d4f5b6"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("rows_filled", sa.Integer()),
    ("rows_missed", sa.Integer()),
    ("result_json", sa.Text()),
)


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    have = _existing_columns("ingest_jobs")
    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column("ingest_jobs", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    have = _existing_columns("ingest_jobs")
    for name, _type in reversed(_COLUMNS):
        if name in have:
            op.drop_column("ingest_jobs", name)
