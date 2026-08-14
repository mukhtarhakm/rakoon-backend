"""Add scan sessions table and scan_session_id column

Revision ID: a1b2c3d4e5f6
Revises: f92b1d567f9b
Create Date: 2026-08-14 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f92b1d567f9b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'scan_sessions',
        sa.Column('id', sa.Uuid(as_uuid=False), nullable=False),
        sa.Column('user_id', sa.Uuid(as_uuid=False), nullable=False),
        sa.Column('store_id', sa.Uuid(as_uuid=False), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['store_id'], ['stores.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scan_sessions_id'), 'scan_sessions', ['id'], unique=False)
    op.create_index(op.f('ix_scan_sessions_user_id'), 'scan_sessions', ['user_id'], unique=False)

    op.add_column('price_entries', sa.Column('scan_session_id', sa.Uuid(as_uuid=False), nullable=True))
    op.create_index(op.f('ix_price_entries_scan_session_id'), 'price_entries', ['scan_session_id'], unique=False)
    op.create_foreign_key('fk_price_entries_scan_session_id', 'price_entries', 'scan_sessions', ['scan_session_id'], ['id'])



def downgrade() -> None:
    op.drop_constraint('fk_price_entries_scan_session_id', 'price_entries', type_='foreignkey')
    op.drop_index(op.f('ix_price_entries_scan_session_id'), table_name='price_entries')
    op.drop_column('price_entries', 'scan_session_id')
    op.drop_index(op.f('ix_scan_sessions_user_id'), table_name='scan_sessions')
    op.drop_index(op.f('ix_scan_sessions_id'), table_name='scan_sessions')
    op.drop_table('scan_sessions')
