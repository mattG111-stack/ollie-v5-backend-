"""add wish_lists (saved searches / watch lists)

Revision ID: a1b2c3d4e5f6
Revises: 0dc8db7893b2
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "0dc8db7893b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wish_lists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("district", sa.String(length=64), nullable=True),
        sa.Column("suburb", sa.String(length=120), nullable=True),
        sa.Column("property_category", sa.String(length=24), nullable=True),
        sa.Column("min_price", sa.Float(), nullable=True),
        sa.Column("max_price", sa.Float(), nullable=True),
        sa.Column("min_beds", sa.Integer(), nullable=True),
        sa.Column("underpriced_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("subdividable_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("max_dev_buy_price", sa.Float(), nullable=True),
        sa.Column("last_seen_batch_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("wish_lists")
