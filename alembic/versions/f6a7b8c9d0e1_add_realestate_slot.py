"""reserve realestate.co.nz estimate columns

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-23
"""
from alembic import op
import sqlalchemy as sa


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

_COLS = [
    ("realestate_valuation", sa.Float()),
    ("realestate_valuation_low", sa.Float()),
    ("realestate_valuation_high", sa.Float()),
    ("realestate_url", sa.String(length=300)),
]


def upgrade() -> None:
    for name, typ in _COLS:
        op.add_column("properties_for_sale", sa.Column(name, typ, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLS):
        op.drop_column("properties_for_sale", name)
