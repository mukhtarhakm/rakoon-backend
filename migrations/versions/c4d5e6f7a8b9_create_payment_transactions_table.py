"""Create payment_transactions table for ad campaign payments

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'ad_campaigns' not in tables:
            raise RuntimeError("Preflight check failed: Table 'ad_campaigns' does not exist.")
        if 'payment_transactions' in tables:
            raise RuntimeError("Preflight check failed: Table 'payment_transactions' already exists.")

    op.create_table(
        'payment_transactions',
        sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True, nullable=False),
        sa.Column('campaign_id', sa.Uuid(as_uuid=False), sa.ForeignKey('ad_campaigns.id'), nullable=False),
        sa.Column('owner_user_id', sa.Uuid(as_uuid=False), nullable=False),
        sa.Column('provider', sa.String(), nullable=False, server_default='xendit'),
        sa.Column('environment', sa.String(), nullable=False, server_default='sandbox'),
        sa.Column('external_id', sa.String(), nullable=False),
        sa.Column('xendit_invoice_id', sa.String(), nullable=True),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(), nullable=False, server_default='IDR'),
        sa.Column('status', sa.String(), nullable=False, server_default='PENDING'),
        sa.Column('invoice_url', sa.String(), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )

    op.create_index('ix_payment_transactions_id', 'payment_transactions', ['id'], unique=False)
    op.create_index('ix_payment_transactions_campaign_id', 'payment_transactions', ['campaign_id'], unique=False)
    op.create_index('ix_payment_transactions_owner_user_id', 'payment_transactions', ['owner_user_id'], unique=False)
    op.create_index('ix_payment_transactions_external_id', 'payment_transactions', ['external_id'], unique=True)
    op.create_index('ix_payment_transactions_xendit_invoice_id', 'payment_transactions', ['xendit_invoice_id'], unique=False)
    op.create_index('ix_payment_transactions_status', 'payment_transactions', ['status'], unique=False)
    op.create_index('ix_payment_transactions_campaign_status', 'payment_transactions', ['campaign_id', 'status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_payment_transactions_campaign_status', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_status', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_xendit_invoice_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_external_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_owner_user_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_campaign_id', table_name='payment_transactions')
    op.drop_index('ix_payment_transactions_id', table_name='payment_transactions')
    op.drop_table('payment_transactions')
