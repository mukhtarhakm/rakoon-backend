"""drop public.users (M5, contract phase)

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-10-16 09:10:00.000000

Irreversible for data: all rows of users are discarded. No code reads or writes the table any
more (admin queue names come from the Supabase Admin API). Check before running in production:
    SELECT count(*) FROM public.users;
    SELECT conrelid::regclass, conname FROM pg_constraint WHERE confrelid = 'public.users'::regclass;
    SELECT * FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
      WHERE d.refobjid = 'public.users'::regclass;   -- views/RLS policies/functions that use it

downgrade() recreates the table empty with the last known columns and indexes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_users() -> bool:
    return sa.inspect(op.get_bind()).has_table("users")


def upgrade() -> None:
    if _has_users():
        op.drop_table("users")  # drops its indexes with it


def downgrade() -> None:
    if _has_users():
        return
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("nama", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("reputasi_score", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("role", sa.String(), nullable=False, server_default="user"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
