"""add breakeven_deposit_pct

The deposit fraction at which a listing's net rent covers its mortgage.
Stored so the deal-finder can sort and filter by "how much cash does this
actually need" rather than a yes/no against one assumed deposit.

Revision ID: b3e7c1d95a20
Revises: a2c5e91b8f40
"""
from alembic import op
import sqlalchemy as sa

revision = "b3e7c1d95a20"
down_revision = "a2c5e91b8f40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale",
                  sa.Column("breakeven_deposit_pct", sa.Float(), nullable=True))
    op.create_index("ix_fs_breakeven_deposit", "properties_for_sale",
                    ["breakeven_deposit_pct"])


def downgrade() -> None:
    op.drop_index("ix_fs_breakeven_deposit", table_name="properties_for_sale")
    op.drop_column("properties_for_sale", "breakeven_deposit_pct")
