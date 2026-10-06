"""Add payment audit fields to ad_campaigns

Revision ID: e6f7a8b9c0d1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-07 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'ad_campaigns' in tables:
        cols = [c['name'] for c in inspector.get_columns('ad_campaigns')]
        with op.batch_alter_table('ad_campaigns') as batch_op:
            if 'payment_method' not in cols:
                batch_op.add_column(sa.Column('payment_method', sa.String(), server_default='QRIS', nullable=False))
            if 'payment_ref' not in cols:
                batch_op.add_column(sa.Column('payment_ref', sa.String(), nullable=True))
                batch_op.create_index(op.f('ix_ad_campaigns_payment_ref'), ['payment_ref'], unique=False)
            if 'payment_status' not in cols:
                batch_op.add_column(sa.Column('payment_status', sa.String(), server_default='paid', nullable=False))
                batch_op.create_index(op.f('ix_ad_campaigns_payment_status'), ['payment_status'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'ad_campaigns' in tables:
        with op.batch_alter_table('ad_campaigns') as batch_op:
            cols = [c['name'] for c in inspector.get_columns('ad_campaigns')]
            if 'payment_status' in cols:
                batch_op.drop_index(op.f('ix_ad_campaigns_payment_status'))
                batch_op.drop_column('payment_status')
            if 'payment_ref' in cols:
                batch_op.drop_index(op.f('ix_ad_campaigns_payment_ref'))
                batch_op.drop_column('payment_ref')
            if 'payment_method' in cols:
                batch_op.drop_column('payment_method')
