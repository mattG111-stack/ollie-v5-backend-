"""add ingest_jobs

Revision ID: d1a2c3b4e5f6
Revises: aae8d7239645
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa


revision = "d1a2c3b4e5f6"
down_revision = "aae8d7239645"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_type", sa.String(16), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.String(1024)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(64)),
        sa.Column("rows_total", sa.Integer()),
        sa.Column("rows_inserted", sa.Integer()),
        sa.Column("rows_rejected", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("import_batches.id")),
        sa.Column("uploaded_by_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ingest_jobs_status", "ingest_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status", "ingest_jobs")
    op.drop_table("ingest_jobs")
