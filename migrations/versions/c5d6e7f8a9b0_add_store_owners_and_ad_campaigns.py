"""Add store_owners and ad_campaigns tables

Revision ID: c5d6e7f8a9b0
Revises: 4bc062a63c8c
Create Date: 2026-10-07 03:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = '4bc062a63c8c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # Create store_owners table if not exists
    if 'store_owners' not in tables:
        op.create_table(
            'store_owners',
            sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
            sa.Column('user_id', sa.Uuid(as_uuid=False), nullable=False),
            sa.Column('store_id', sa.Uuid(as_uuid=False), nullable=False),
            sa.Column('status', sa.String(), server_default='verified', nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['store_id'], ['stores.id']),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_store_owners_id'), 'store_owners', ['id'], unique=False)
        op.create_index(op.f('ix_store_owners_user_id'), 'store_owners', ['user_id'], unique=False)
        op.create_index(op.f('ix_store_owners_store_id'), 'store_owners', ['store_id'], unique=False)

    # Create ad_campaigns table if not exists
    if 'ad_campaigns' not in tables:
        op.create_table(
            'ad_campaigns',
            sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
            sa.Column('store_id', sa.Uuid(as_uuid=False), nullable=False),
            sa.Column('owner_user_id', sa.Uuid(as_uuid=False), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('banner_url', sa.String(), nullable=False),
            sa.Column('duration_days', sa.Integer(), server_default='3', nullable=False),
            sa.Column('price_paid', sa.Integer(), server_default='15000', nullable=False),
            sa.Column('status', sa.String(), server_default='active', nullable=False),
            sa.Column('start_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(['store_id'], ['stores.id']),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_ad_campaigns_id'), 'ad_campaigns', ['id'], unique=False)
        op.create_index(op.f('ix_ad_campaigns_store_id'), 'ad_campaigns', ['store_id'], unique=False)
        op.create_index(op.f('ix_ad_campaigns_owner_user_id'), 'ad_campaigns', ['owner_user_id'], unique=False)
        op.create_index(op.f('ix_ad_campaigns_status'), 'ad_campaigns', ['status'], unique=False)
        op.create_index(op.f('ix_ad_campaigns_expires_at'), 'ad_campaigns', ['expires_at'], unique=False)
        op.create_index('ix_ad_campaigns_status_expires', 'ad_campaigns', ['status', 'expires_at'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if 'ad_campaigns' in tables:
        op.drop_index('ix_ad_campaigns_status_expires', table_name='ad_campaigns')
        op.drop_index(op.f('ix_ad_campaigns_expires_at'), table_name='ad_campaigns')
        op.drop_index(op.f('ix_ad_campaigns_status'), table_name='ad_campaigns')
        op.drop_index(op.f('ix_ad_campaigns_owner_user_id'), table_name='ad_campaigns')
        op.drop_index(op.f('ix_ad_campaigns_store_id'), table_name='ad_campaigns')
        op.drop_index(op.f('ix_ad_campaigns_id'), table_name='ad_campaigns')
        op.drop_table('ad_campaigns')

    if 'store_owners' in tables:
        op.drop_index(op.f('ix_store_owners_store_id'), table_name='store_owners')
        op.drop_index(op.f('ix_store_owners_user_id'), table_name='store_owners')
        op.drop_index(op.f('ix_store_owners_id'), table_name='store_owners')
        op.drop_table('store_owners')
