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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'scan_sessions' not in tables:
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

    pe_cols = [c['name'] for c in inspector.get_columns('price_entries')]
    if 'scan_session_id' not in pe_cols:
        with op.batch_alter_table('price_entries') as batch_op:
            batch_op.add_column(sa.Column('scan_session_id', sa.Uuid(as_uuid=False), nullable=True))
            batch_op.create_index(op.f('ix_price_entries_scan_session_id'), ['scan_session_id'], unique=False)
            batch_op.create_foreign_key('fk_price_entries_scan_session_id', 'scan_sessions', ['scan_session_id'], ['id'])



def downgrade() -> None:
    with op.batch_alter_table('price_entries') as batch_op:
        batch_op.drop_constraint('fk_price_entries_scan_session_id', type_='foreignkey')
        batch_op.drop_index(op.f('ix_price_entries_scan_session_id'))
        batch_op.drop_column('scan_session_id')
    op.drop_index(op.f('ix_scan_sessions_user_id'), table_name='scan_sessions')
    op.drop_index(op.f('ix_scan_sessions_id'), table_name='scan_sessions')
    op.drop_table('scan_sessions')
