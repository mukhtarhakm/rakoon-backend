"""price_entries.store_id foreign key and store_submissions.created_store_id index

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-10-15 09:20:00.000000

Production already has price_entries_store_id_fkey (created outside Alembic, audit P2-02), so the
FK is only added when no foreign key covers price_entries.store_id. Adding it to a database that
does hold orphan rows fails; check first:
    SELECT count(*) FROM price_entries pe LEFT JOIN stores s ON s.id = pe.store_id WHERE s.id IS NULL;

downgrade() drops only the index: dropping the FK would remove an integrity guarantee that
production had before this migration existed.
"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b0c1d2e3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK = 'price_entries_store_id_fkey'
INDEX = 'ix_store_submissions_created_store_id'


def upgrade() -> None:
    has_fk, has_index = False, False
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        has_fk = any(
            fk["constrained_columns"] == ["store_id"] and fk["referred_table"] == "stores"
            for fk in inspector.get_foreign_keys("price_entries")
        )
        has_index = INDEX in {i["name"] for i in inspector.get_indexes("store_submissions")}

    if not has_fk:
        with op.batch_alter_table("price_entries") as batch_op:
            batch_op.create_foreign_key(FK, "stores", ["store_id"], ["id"])
    if not has_index:
        op.create_index(INDEX, "store_submissions", ["created_store_id"], unique=False)


def downgrade() -> None:
    has_index = True
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        has_index = INDEX in {i["name"] for i in inspector.get_indexes("store_submissions")}
    if has_index:
        op.drop_index(INDEX, table_name="store_submissions")
