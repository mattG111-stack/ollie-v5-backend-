"""add land-area verification columns (weekly pre-publish check)

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale",
                  sa.Column("land_area_listing_m2", sa.Float(), nullable=True))
    op.add_column("properties_for_sale",
                  sa.Column("land_area_flag", sa.String(length=16), nullable=True))
    op.create_index("ix_fs_land_area_flag", "properties_for_sale", ["land_area_flag"])


def downgrade() -> None:
    op.drop_index("ix_fs_land_area_flag", table_name="properties_for_sale")
    op.drop_column("properties_for_sale", "land_area_flag")
    op.drop_column("properties_for_sale", "land_area_listing_m2")
