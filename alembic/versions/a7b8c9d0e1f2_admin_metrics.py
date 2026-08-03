"""admin metrics: login_count, stripe_customer_id, agent_contacts

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("login_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(length=64), nullable=True))
    op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"])

    op.create_table(
        "agent_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("property_id", sa.Integer(), nullable=True, index=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("suburb", sa.String(length=120), nullable=True),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("agent_contacts")
    op.drop_index("ix_users_stripe_customer_id", table_name="users")
    op.drop_column("users", "stripe_customer_id")
    op.drop_column("users", "login_count")
