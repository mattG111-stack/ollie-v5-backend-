"""v4 deal-finding: fair_value + margin columns

Revision ID: d7f3a92e4c18
Revises: c4d8e1f72b56
Create Date: 2026-06-27
"""
from alembic import op
import sqlalchemy as sa


revision = "d7f3a92e4c18"
down_revision = "c4d8e1f72b56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("fair_value", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("margin", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "margin")
    op.drop_column("properties_for_sale", "fair_value")
