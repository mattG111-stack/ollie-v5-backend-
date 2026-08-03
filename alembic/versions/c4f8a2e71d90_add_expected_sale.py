"""add expected_sale, expected_sale_path, expected_sale_band

Two different questions need two different numbers (as in the v4 tool, which
shows "WHAT TO PAY" and "WHAT IS IT WORTH" side by side):

  expected_sale  what this will transact at. Uses asking x 0.95 when the vendor
                 has published a price -- that is the strongest signal there is
                 -- and falls back to CV x the area sale/CV ratio when there is
                 no price.
  fair_value     what it is worth, computed WITHOUT the asking price, so the
                 margin means something.

expected_sale_band records the confidence: +/-4% on the listed path, +/-14% on
the unlisted one. Valuing from an asking price is far tighter than valuing from
scratch and the UI must not imply otherwise.

Revision ID: c4f8a2e71d90
Revises: b3e7c1d95a20
"""
from alembic import op
import sqlalchemy as sa

revision = "c4f8a2e71d90"
down_revision = "b3e7c1d95a20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("properties_for_sale", sa.Column("expected_sale", sa.Float(), nullable=True))
    op.add_column("properties_for_sale", sa.Column("expected_sale_path", sa.String(16), nullable=True))
    op.add_column("properties_for_sale", sa.Column("expected_sale_band", sa.Float(), nullable=True))
    op.create_index("ix_fs_expected_sale", "properties_for_sale", ["expected_sale"])


def downgrade() -> None:
    op.drop_index("ix_fs_expected_sale", table_name="properties_for_sale")
    for c in ("expected_sale_band", "expected_sale_path", "expected_sale"):
        op.drop_column("properties_for_sale", c)
