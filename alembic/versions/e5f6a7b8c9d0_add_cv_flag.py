"""add cv_flag — CV reconciliation vs homes.co.nz

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("cv_flag", sa.String(length=16), nullable=True))
    op.create_index("ix_fs_cv_flag", "properties_for_sale", ["cv_flag"])


def downgrade() -> None:
    op.drop_index("ix_fs_cv_flag", table_name="properties_for_sale")
    op.drop_column("properties_for_sale", "cv_flag")
