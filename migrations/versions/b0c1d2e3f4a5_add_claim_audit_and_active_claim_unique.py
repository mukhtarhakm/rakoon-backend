"""Claim audit columns and one active claim per (user, store)

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-10-15 09:10:00.000000

Adds store_owners.reviewed_by / reviewed_at (only those that do not exist yet) and a partial
unique index so a user can hold at most one pending-or-verified claim per store. Rejected rows
stay as history and may repeat.

Run before upgrading production (the migration refuses to continue if it returns rows):
    SELECT user_id, store_id, count(*) FROM store_owners
    WHERE status IN ('pending', 'verified') GROUP BY user_id, store_id HAVING count(*) > 1;
"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0c1d2e3f4a5'
down_revision: Union[str, Sequence[str], None] = 'a9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX = 'uq_store_owners_active_user_store'
ACTIVE = "status IN ('pending', 'verified')"


def upgrade() -> None:
    columns, indexes = set(), set()
    if not context.is_offline_mode():
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        columns = {c["name"] for c in inspector.get_columns("store_owners")}
        indexes = {i["name"] for i in inspector.get_indexes("store_owners")}

        dupes = bind.execute(sa.text(
            "SELECT count(*) FROM ("
            " SELECT user_id, store_id FROM store_owners"
            f" WHERE {ACTIVE} GROUP BY user_id, store_id HAVING count(*) > 1"
            ") dupes"
        )).scalar()
        if dupes:
            raise RuntimeError(
                f"Preflight check failed: {dupes} (user, store) pair(s) have more than one pending/verified "
                "claim. Resolve them before creating the unique index."
            )

    new_columns = []
    if "reviewed_by" not in columns:
        new_columns.append(sa.Column("reviewed_by", sa.String(), nullable=True))
    if "reviewed_at" not in columns:
        new_columns.append(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    if new_columns:
        with op.batch_alter_table("store_owners") as batch_op:
            for column in new_columns:
                batch_op.add_column(column)

    if INDEX not in indexes:
        op.create_index(
            INDEX,
            'store_owners',
            ['user_id', 'store_id'],
            unique=True,
            postgresql_where=sa.text(ACTIVE),
            sqlite_where=sa.text(ACTIVE),
        )


def downgrade() -> None:
    columns, indexes = {"reviewed_by", "reviewed_at"}, {INDEX}
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        columns = {c["name"] for c in inspector.get_columns("store_owners")}
        indexes = {i["name"] for i in inspector.get_indexes("store_owners")}

    if INDEX in indexes:
        op.drop_index(INDEX, table_name='store_owners')
    drop = [name for name in ("reviewed_at", "reviewed_by") if name in columns]
    if drop:
        with op.batch_alter_table("store_owners") as batch_op:
            for name in drop:
                batch_op.drop_column(name)
