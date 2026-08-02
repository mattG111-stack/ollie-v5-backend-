"""self-serve onboarding: verification + trial billing

Adds onboarding/subscription columns to users and the verification_codes table.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("subscription_status", sa.String(length=24), nullable=True))
    op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("signup_source", sa.String(length=16), nullable=True))
    op.create_index("ix_users_subscription_status", "users", ["subscription_status"])

    op.create_table(
        "verification_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Existing accounts predate self-serve signup — mark them as admin-origin so
    # they keep working and don't show up in the self-serve funnel counts.
    op.execute("UPDATE users SET signup_source = 'admin' WHERE signup_source IS NULL")


def downgrade() -> None:
    op.drop_table("verification_codes")
    op.drop_index("ix_users_subscription_status", table_name="users")
    op.drop_column("users", "signup_source")
    op.drop_column("users", "current_period_end")
    op.drop_column("users", "trial_ends_at")
    op.drop_column("users", "subscription_status")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "email_verified_at")
