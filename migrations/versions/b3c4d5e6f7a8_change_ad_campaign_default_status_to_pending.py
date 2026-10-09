"""Change ad_campaigns default status to pending_payment and payment_status to unpaid

Revision ID: b3c4d5e6f7a8
Revises: e8f9a0b1c2d3
Create Date: 2026-10-10 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'e8f9a0b1c2d3'
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

    # Alter server defaults for ad_campaigns
    with op.batch_alter_table('ad_campaigns') as batch_op:
        batch_op.alter_column(
            'status',
            server_default='pending_payment',
            existing_type=sa.String(),
            existing_nullable=False
        )
        batch_op.alter_column(
            'payment_status',
            server_default='unpaid',
            existing_type=sa.String(),
            existing_nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table('ad_campaigns') as batch_op:
        batch_op.alter_column(
            'payment_status',
            server_default='paid',
            existing_type=sa.String(),
            existing_nullable=False
        )
        batch_op.alter_column(
            'status',
            server_default='active',
            existing_type=sa.String(),
            existing_nullable=False
        )