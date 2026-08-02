"""two-stage publish: batch status + held rows

Adds ImportBatch.status/published_at and PropertyForSale.is_held/hold_reason.
Backfills existing batches so current live data keeps serving.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_batches", sa.Column("status", sa.String(length=16), nullable=False, server_default="staged"))
    op.add_column("import_batches", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_import_batches_status", "import_batches", ["status"])
    op.add_column("properties_for_sale", sa.Column("is_held", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("properties_for_sale", sa.Column("hold_reason", sa.String(length=300), nullable=True))
    op.create_index("ix_properties_for_sale_is_held", "properties_for_sale", ["is_held"])

    # Backfill: whatever is live now is 'published'; older inactive batches are
    # 'archived'. This keeps the current site serving after the migration.
    op.execute("UPDATE import_batches SET status = 'published', published_at = created_at WHERE is_active = true")
    op.execute("UPDATE import_batches SET status = 'archived' WHERE is_active = false")


def downgrade() -> None:
    op.drop_index("ix_properties_for_sale_is_held", table_name="properties_for_sale")
    op.drop_column("properties_for_sale", "hold_reason")
    op.drop_column("properties_for_sale", "is_held")
    op.drop_index("ix_import_batches_status", table_name="import_batches")
    op.drop_column("import_batches", "published_at")
    op.drop_column("import_batches", "status")
