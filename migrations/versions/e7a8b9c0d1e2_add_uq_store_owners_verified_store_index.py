"""Add partial unique index on store_owners for verified store

Revision ID: e7a8b9c0d1e2
Revises: d5e6f7a8b9c0
Create Date: 2026-10-11 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
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

        # Check for duplicate verified claims before adding unique index
        dup_query = sa.text("""
            SELECT count(*) FROM (
                SELECT store_id
                FROM store_owners
                WHERE status = 'verified' AND verified_at IS NOT NULL
                GROUP BY store_id
                HAVING count(*) > 1
            ) dupes;
        """)
        dup_count = bind.execute(dup_query).scalar()
        if dup_count and dup_count > 0:
            raise RuntimeError(
                f"Preflight check failed: Found {dup_count} store(s) with multiple verified owners. "
                "Resolve duplicate verified claims before creating unique index."
            )

        existing_indexes = [idx['name'] for idx in inspector.get_indexes('store_owners')]
        if 'uq_store_owners_verified_store' in existing_indexes:
            return

    op.create_index(
        'uq_store_owners_verified_store',
        'store_owners',
        ['store_id'],
        unique=True,
        postgresql_where=sa.text("status = 'verified' AND verified_at IS NOT NULL"),
        sqlite_where=sa.text("status = 'verified' AND verified_at IS NOT NULL"),
    )


def downgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'store_owners' not in tables:
            return
        existing_indexes = [idx['name'] for idx in inspector.get_indexes('store_owners')]
        if 'uq_store_owners_verified_store' not in existing_indexes:
            return

    op.drop_index(
        'uq_store_owners_verified_store',
        table_name='store_owners',
    )
