"""acquisition layer: buy_price, area_value, comp_tier, comps_matched

Revision ID: e8b1c4a6f902
Revises: d7f3a92e4c18
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa


revision = "e8b1c4a6f902"
down_revision = "d7f3a92e4c18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("buy_price", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("area_value", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("comp_tier", sa.Integer(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("comps_matched", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "comps_matched")
    op.drop_column("properties_for_sale", "comp_tier")
    op.drop_column("properties_for_sale", "area_value")
    op.drop_column("properties_for_sale", "buy_price")
