"""staged ingest: durable job progress + result column

Adds rows_filled / rows_missed (durable CoreLogic enrich counters), and
result_json (structured stage results — e.g. the publish result dict, which was
previously serialised into the varchar(64) `stage` label and raised
StringDataRightTruncation).

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


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("rows_filled", sa.Integer(), nullable=True))
    op.add_column("ingest_jobs", sa.Column("rows_missed", sa.Integer(), nullable=True))
    op.add_column("ingest_jobs", sa.Column("result_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingest_jobs", "result_json")
    op.drop_column("ingest_jobs", "rows_missed")
    op.drop_column("ingest_jobs", "rows_filled")
