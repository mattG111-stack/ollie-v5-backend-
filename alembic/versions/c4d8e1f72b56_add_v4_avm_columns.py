"""v4 AVM columns: listing_type, pricing_path, ranges, subdivision premium

Revision ID: c4d8e1f72b56
Revises: f2a4e8b91c33
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa


revision = "c4d8e1f72b56"
down_revision = "f2a4e8b91c33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("listing_type", sa.String(16), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pricing_path", sa.String(16), nullable=True))
    op.add_column("properties_for_sale", sa.Column("range_low", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("range_high", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("subdivision_premium", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "subdivision_premium")
    op.drop_column("properties_for_sale", "range_high")
    op.drop_column("properties_for_sale", "range_low")
    op.drop_column("properties_for_sale", "pricing_path")
    op.drop_column("properties_for_sale", "listing_type")
