"""Add rejection_reason column to store_owners table

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-10-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'store_owners' not in tables:
            raise RuntimeError("Preflight check failed: Table 'store_owners' does not exist.")
        columns = [c['name'] for c in inspector.get_columns('store_owners')]
        if 'rejection_reason' in columns:
            return

    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.add_column(sa.Column("rejection_reason", sa.String(), nullable=True))


def downgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'store_owners' not in tables:
            return
        columns = [c['name'] for c in inspector.get_columns('store_owners')]
        if 'rejection_reason' not in columns:
            return

    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.drop_column("rejection_reason")
