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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'users' in tables:
        columns = [c['name'] for c in inspector.get_columns('users')]
        if 'role' not in columns:
            op.add_column('users', sa.Column('role', sa.String(), server_default='user', nullable=False))
    else:
        op.create_table(
            'users',
            sa.Column('id', sa.String(), nullable=False),
            sa.Column('nama', sa.String(), nullable=False),
            sa.Column('email', sa.String(), nullable=False),
            sa.Column('reputasi_score', sa.Integer(), nullable=True, server_default='0'),
            sa.Column('role', sa.String(), server_default='user', nullable=False),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
        op.create_index(op.f('ix_users_id'), 'users', ['id'], unique=False)

    if 'products' in tables:
        columns = [c['name'] for c in inspector.get_columns('products')]
        if 'foto_url' not in columns:
            op.add_column('products', sa.Column('foto_url', sa.String(), nullable=True))
    else:
        op.add_column('products', sa.Column('foto_url', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'products' in tables:
        columns = [c['name'] for c in inspector.get_columns('products')]
        if 'foto_url' in columns:
            op.drop_column('products', 'foto_url')

    if 'users' in tables:
        columns = [c['name'] for c in inspector.get_columns('users')]
        if 'role' in columns:
            op.drop_column('users', 'role')
