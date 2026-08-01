"""propertyvalue.co.nz (CoreLogic) on-demand cache columns

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("pv_estimate_low", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_estimate_high", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_estimate_mid", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_cv", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_url", sa.String(length=400), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_data", sa.Text(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_checked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for col in ("pv_checked_at", "pv_data", "pv_url", "pv_cv",
                "pv_estimate_mid", "pv_estimate_high", "pv_estimate_low"):
        op.drop_column("properties_for_sale", col)
