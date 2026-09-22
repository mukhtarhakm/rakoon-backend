"""add_composite_indexes_to_price_entries

Revision ID: 720dfaf988a6
Revises: b2c3d4e5f6a7
Create Date: 2026-09-22 23:31:07.983132

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '720dfaf988a6'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add composite and single-column performance indexes on price_entries."""
    op.create_index(
        'ix_price_entries_product_status',
        'price_entries',
        ['product_id', 'status_verifikasi'],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        'ix_price_entries_store_product',
        'price_entries',
        ['store_id', 'product_id'],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        'ix_price_entries_timestamp',
        'price_entries',
        ['timestamp'],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        'ix_price_entries_status_verifikasi',
        'price_entries',
        ['status_verifikasi'],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        'ix_price_entries_store_id',
        'price_entries',
        ['store_id'],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        'ix_price_entries_product_id',
        'price_entries',
        ['product_id'],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    """Remove composite and single-column performance indexes on price_entries."""
    op.drop_index('ix_price_entries_product_id', table_name='price_entries', if_exists=True)
    op.drop_index('ix_price_entries_store_id', table_name='price_entries', if_exists=True)
    op.drop_index('ix_price_entries_status_verifikasi', table_name='price_entries', if_exists=True)
    op.drop_index('ix_price_entries_timestamp', table_name='price_entries', if_exists=True)
    op.drop_index('ix_price_entries_store_product', table_name='price_entries', if_exists=True)
    op.drop_index('ix_price_entries_product_status', table_name='price_entries', if_exists=True)
