"""add per-user llm credentials

Revision ID: 0dc8db7893b2
Revises: b56603160803
Create Date: 2026-07-20 19:14:58.737961

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0dc8db7893b2'
down_revision: Union[str, None] = 'b56603160803'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('llm_provider', sa.String(length=16), nullable=True))
    op.add_column('users', sa.Column('llm_api_key_encrypted', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('llm_key_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'llm_key_updated_at')
    op.drop_column('users', 'llm_api_key_encrypted')
    op.drop_column('users', 'llm_provider')
