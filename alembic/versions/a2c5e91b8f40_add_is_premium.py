"""add is_premium flag (ultra-prime: priced off listing, no model valuation)

Revision ID: a2c5e91b8f40
Revises: f1a9d3c75e44
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa


revision = "a2c5e91b8f40"
down_revision = "f1a9d3c75e44"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("is_premium", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "is_premium")
