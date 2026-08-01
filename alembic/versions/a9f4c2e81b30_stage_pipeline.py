"""Four-stage operator-triggered ingest: load / enrich / price / publish.

Adds durable, database-resident progress so a stage's state survives the browser
disconnecting, the tab losing focus, or a page refresh.

ingest_jobs gains:
  stage_name        which of the four stages this job is
  rows_processed    counter that advances while the stage runs
  rows_filled       enrich: rows CoreLogic returned usable data for
  rows_missed       enrich: rows CoreLogic returned nothing for (NOT a failure)
  rows_skipped      enrich: rows that needed no lookup
  cancel_requested  co-operative cancellation flag, polled by the worker
  heartbeat_at      last time the worker touched this row — distinguishes
                    "still running" from "died forty minutes ago"

properties_for_sale gains per-row enrich state so a re-run RESUMES rather than
repeating work already paid for in CoreLogic calls and wall-clock time.

Revision ID: a9f4c2e81b30
Revises: f2a3b4c5d6e7
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9f4c2e81b30"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("stage_name", sa.String(16), nullable=True))
    op.create_index("ix_ingest_jobs_stage_name", "ingest_jobs", ["stage_name"])
    op.add_column("ingest_jobs", sa.Column("rows_processed", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ingest_jobs", sa.Column("rows_filled", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ingest_jobs", sa.Column("rows_missed", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ingest_jobs", sa.Column("rows_skipped", sa.Integer(), server_default="0", nullable=False))
    op.add_column("ingest_jobs", sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("ingest_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))

    # Existing jobs predate staging; label them so the admin history isn't blank.
    op.execute("UPDATE ingest_jobs SET stage_name = 'legacy' WHERE stage_name IS NULL")

    op.add_column(
        "properties_for_sale",
        sa.Column("enrich_status", sa.String(16), server_default="pending", nullable=False),
    )
    op.create_index("ix_properties_for_sale_enrich_status", "properties_for_sale", ["enrich_status"])
    op.add_column("properties_for_sale", sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("properties_for_sale", sa.Column("priced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("properties_for_sale", sa.Column("enrich_cells_filled", sa.Integer(), server_default="0", nullable=False))

    # Rows already in the database went through the old welded pipeline, so they
    # are both enriched and priced. Marking them 'skipped' rather than 'pending'
    # stops a future enrich run from re-paying for lookups already made.
    op.execute("UPDATE properties_for_sale SET enrich_status = 'skipped'")


def downgrade() -> None:
    op.drop_column("properties_for_sale", "enrich_cells_filled")
    op.drop_column("properties_for_sale", "priced_at")
    op.drop_column("properties_for_sale", "enriched_at")
    op.drop_index("ix_properties_for_sale_enrich_status", table_name="properties_for_sale")
    op.drop_column("properties_for_sale", "enrich_status")

    op.drop_column("ingest_jobs", "heartbeat_at")
    op.drop_column("ingest_jobs", "cancel_requested")
    op.drop_column("ingest_jobs", "rows_skipped")
    op.drop_column("ingest_jobs", "rows_missed")
    op.drop_column("ingest_jobs", "rows_filled")
    op.drop_column("ingest_jobs", "rows_processed")
    op.drop_index("ix_ingest_jobs_stage_name", table_name="ingest_jobs")
    op.drop_column("ingest_jobs", "stage_name")
