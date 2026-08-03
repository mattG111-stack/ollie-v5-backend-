"""widen expected_sale_path to 32 chars

The matched-sold-price path records which tier produced the value
("matched_sold:suburb_land_floor"), which overflows varchar(16). The tier is
worth keeping — it says how like-for-like the comps actually were.

Revision ID: d5a1c93e6b47
Revises: c4f8a2e71d90
"""
from alembic import op
import sqlalchemy as sa

revision = "d5a1c93e6b47"
down_revision = "c4f8a2e71d90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("properties_for_sale", "expected_sale_path",
                    type_=sa.String(32), existing_type=sa.String(16))


def downgrade() -> None:
    op.alter_column("properties_for_sale", "expected_sale_path",
                    type_=sa.String(16), existing_type=sa.String(32))
