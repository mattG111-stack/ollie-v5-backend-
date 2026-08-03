"""add image_urls gallery column

Stores every listing photo (newline-separated) rather than just the first.
The scrape carries up to 20 per listing; only image_1_url was being kept.

Added to all three property tables because the column lives on the shared
property mixin — leaving sold/rent out would drift the model from the schema.

Autogenerate also wanted to drop and recreate ix_fs_breakeven_deposit and
ix_fs_expected_sale under Alembic's default naming convention. That is
pre-existing naming drift, unrelated to this change, and rebuilding indexes on
a 131k-row table as a side effect of adding a text column is not worth it.
Deliberately omitted.

Revision ID: b56603160803
Revises: d5a1c93e6b47
Create Date: 2026-07-20 16:54:04.116085

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b56603160803'
down_revision: Union[str, None] = 'd5a1c93e6b47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('properties_for_sale', sa.Column('image_urls', sa.Text(), nullable=True))
    op.add_column('properties_rent', sa.Column('image_urls', sa.Text(), nullable=True))
    op.add_column('properties_sold', sa.Column('image_urls', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('properties_sold', 'image_urls')
    op.drop_column('properties_rent', 'image_urls')
    op.drop_column('properties_for_sale', 'image_urls')
