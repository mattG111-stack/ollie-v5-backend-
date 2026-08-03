"""subdivision §6: sections, dwellings, section_rate, gross_sales, subdivision_profit

Revision ID: f1a9d3c75e44
Revises: e8b1c4a6f902
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a9d3c75e44"
down_revision = "e8b1c4a6f902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("sections", sa.Integer(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("dwellings", sa.Integer(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("section_rate", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("gross_sales", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("subdivision_profit", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("properties_for_sale", "subdivision_profit")
    op.drop_column("properties_for_sale", "gross_sales")
    op.drop_column("properties_for_sale", "section_rate")
    op.drop_column("properties_for_sale", "dwellings")
    op.drop_column("properties_for_sale", "sections")
