"""add ingest_jobs.audit_warnings

Revision ID: f2a4e8b91c33
Revises: 90b127cf0322
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


revision = "f2a4e8b91c33"
down_revision = "90b127cf0322"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ingest_jobs", sa.Column("audit_warnings", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ingest_jobs", "audit_warnings")
