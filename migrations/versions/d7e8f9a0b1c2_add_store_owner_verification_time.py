"""Require an admin approval timestamp for store ownership.

Revision ID: d7e8f9a0b1c2
Revises: e6f7a8b9c0d1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.alter_column("status", server_default="pending", existing_type=sa.String(), existing_nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.alter_column("status", server_default="verified", existing_type=sa.String(), existing_nullable=False)
        batch_op.drop_column("verified_at")
