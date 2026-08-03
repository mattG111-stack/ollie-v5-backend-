"""propertyvalue.co.nz last-sale columns

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("pv_last_sale_price", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("pv_last_sale_date", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "pv_last_sale_date")
    op.drop_column("properties_for_sale", "pv_last_sale_price")
