"""cache on-demand homes.co.nz external estimate per property

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_COLS = [
    ("homes_valuation", sa.Float()),
    ("homes_valuation_low", sa.Float()),
    ("homes_valuation_high", sa.Float()),
    ("homes_cv", sa.Float()),
    ("homes_url", sa.String(length=300)),
    ("homes_checked_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    for name, typ in _COLS:
        op.add_column("properties_for_sale", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("properties_for_sale", name)
