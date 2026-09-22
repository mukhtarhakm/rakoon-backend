"""add_admin_role_and_product_photo

Revision ID: 4bc062a63c8c
Revises: 720dfaf988a6
Create Date: 2026-09-23 00:04:01.330603

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4bc062a63c8c'
down_revision: Union[str, Sequence[str], None] = '720dfaf988a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('role', sa.String(), server_default='user', nullable=False))
    op.add_column('products', sa.Column('foto_url', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('products', 'foto_url')
    op.drop_column('users', 'role')
