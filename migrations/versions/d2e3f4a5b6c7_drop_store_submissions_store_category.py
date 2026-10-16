"""drop store_submissions.store_category (M1b, contract phase)

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-16 09:00:00.000000

Irreversible for data: the column's values are discarded. The app no longer reads or writes it
(KEPUTUSAN.md). Check before running in production:
    SELECT store_category, count(*) FROM store_submissions GROUP BY 1;

downgrade() restores the column empty (nullable, no default); old values are not recovered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    inspector = sa.inspect(op.get_bind())
    return "store_category" in {c["name"] for c in inspector.get_columns("store_submissions")}


def upgrade() -> None:
    if _has_column():
        with op.batch_alter_table("store_submissions") as batch_op:
            batch_op.drop_column("store_category")


def downgrade() -> None:
    if not _has_column():
        with op.batch_alter_table("store_submissions") as batch_op:
            batch_op.add_column(sa.Column("store_category", sa.String(), nullable=True))
