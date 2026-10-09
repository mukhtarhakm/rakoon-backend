"""fix schema drift and constraints

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-10-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, Sequence[str], None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    # -------------------------------------------------------------------------
    # Preflight Checks (Online Mode only)
    # -------------------------------------------------------------------------
    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()

        # 1. Verify required tables exist
        if 'products' not in tables:
            raise RuntimeError("Preflight check failed: Table 'products' does not exist.")
        if 'price_entries' not in tables:
            raise RuntimeError("Preflight check failed: Table 'price_entries' does not exist.")

        # 2. Check for exact duplicate product names (which directly violates UNIQUE constraint)
        exact_dup_query = sa.text("""
            SELECT count(*) FROM (
                SELECT nama
                FROM products
                GROUP BY nama
                HAVING count(*) > 1
            ) dupes;
        """)
        exact_dup_count = bind.execute(exact_dup_query).scalar()
        if exact_dup_count and exact_dup_count > 0:
            raise RuntimeError(
                f"Preflight check failed: Found {exact_dup_count} exact duplicate product name(s) in 'products'. "
                "Deduplicate records before applying unique index."
            )

        # 3. Check for case-insensitive / trimmed duplicate names (data hygiene sanity check)
        ci_dup_query = sa.text("""
            SELECT count(*) FROM (
                SELECT lower(trim(nama))
                FROM products
                GROUP BY lower(trim(nama))
                HAVING count(*) > 1
            ) dupes;
        """)
        ci_dup_count = bind.execute(ci_dup_query).scalar()
        if ci_dup_count and ci_dup_count > 0:
            raise RuntimeError(
                f"Preflight check failed: Found {ci_dup_count} case-insensitive/untrimmed collision(s) in 'products'. "
                "Resolve naming collisions before applying unique index."
            )

        # 4. Check for NULL values in columns that will become NOT NULL
        null_entries_query = sa.text("""
            SELECT count(*)
            FROM price_entries
            WHERE product_id IS NULL OR store_id IS NULL OR sumber_user_id IS NULL;
        """)
        null_count = bind.execute(null_entries_query).scalar()
        if null_count and null_count > 0:
            raise RuntimeError(
                f"Preflight check failed: Found {null_count} row(s) in 'price_entries' with NULL "
                "product_id, store_id, or sumber_user_id. Clean or populate invalid rows before applying NOT NULL."
            )

    # -------------------------------------------------------------------------
    # 1. products: Add unique index on nama
    # -------------------------------------------------------------------------
    if is_offline:
        op.create_index(op.f('ix_products_nama'), 'products', ['nama'], unique=True)
    else:
        prod_indexes = [idx['name'] for idx in inspector.get_indexes('products')]
        if 'ix_products_nama' not in prod_indexes:
            op.create_index(op.f('ix_products_nama'), 'products', ['nama'], unique=True)

    # -------------------------------------------------------------------------
    # 2. price_entries: Drop redundant indexes (idx_price_entries_product & idx_price_entries_store)
    #    Managed counterparts ix_price_entries_product_id & ix_price_entries_store_id remain intact.
    # -------------------------------------------------------------------------
    if is_offline:
        op.drop_index('idx_price_entries_product', table_name='price_entries', if_exists=True)
        op.drop_index('idx_price_entries_store', table_name='price_entries', if_exists=True)
    else:
        pe_indexes = [idx['name'] for idx in inspector.get_indexes('price_entries')]
        if 'idx_price_entries_product' in pe_indexes:
            op.drop_index('idx_price_entries_product', table_name='price_entries')
        if 'idx_price_entries_store' in pe_indexes:
            op.drop_index('idx_price_entries_store', table_name='price_entries')

    # -------------------------------------------------------------------------
    # 3. price_entries: Set NOT NULL on product_id, store_id, sumber_user_id
    # -------------------------------------------------------------------------
    with op.batch_alter_table('price_entries') as batch_op:
        batch_op.alter_column(
            'product_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=False
        )
        batch_op.alter_column(
            'store_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=False
        )
        batch_op.alter_column(
            'sumber_user_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=False
        )


def downgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    # 1. Revert NOT NULL constraints
    with op.batch_alter_table('price_entries') as batch_op:
        batch_op.alter_column(
            'sumber_user_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=True
        )
        batch_op.alter_column(
            'store_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=True
        )
        batch_op.alter_column(
            'product_id',
            existing_type=sa.Uuid(as_uuid=False),
            nullable=True
        )

    # 2. Re-create dropped redundant indexes if needed
    if is_offline:
        op.create_index('idx_price_entries_store', 'price_entries', ['store_id'], unique=False)
        op.create_index('idx_price_entries_product', 'price_entries', ['product_id'], unique=False)
        op.drop_index(op.f('ix_products_nama'), table_name='products', if_exists=True)
    else:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()

        if 'price_entries' in tables:
            pe_indexes = [idx['name'] for idx in inspector.get_indexes('price_entries')]
            if 'idx_price_entries_store' not in pe_indexes:
                op.create_index('idx_price_entries_store', 'price_entries', ['store_id'], unique=False)
            if 'idx_price_entries_product' not in pe_indexes:
                op.create_index('idx_price_entries_product', 'price_entries', ['product_id'], unique=False)

        # 3. Drop unique index on products.nama
        if 'products' in tables:
            prod_indexes = [idx['name'] for idx in inspector.get_indexes('products')]
            if 'ix_products_nama' in prod_indexes:
                op.drop_index(op.f('ix_products_nama'), table_name='products')