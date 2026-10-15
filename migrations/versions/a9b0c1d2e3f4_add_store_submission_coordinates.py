"""Add latitude/longitude to store_submissions (applicant-chosen coordinates)

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-10-15 09:00:00.000000

Expand phase only: columns are nullable so rows and code from before this migration keep working.
store_category is intentionally NOT dropped here (contract phase, after the new code is proven).
"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, Sequence[str], None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("latitude", "longitude")


def _existing_columns() -> set:
    if context.is_offline_mode():
        return set()
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("store_submissions")}


def upgrade() -> None:
    existing = _existing_columns()
    missing = [name for name in _COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("store_submissions") as batch_op:
        for name in missing:
            batch_op.add_column(sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    present = [name for name in _COLUMNS if name in existing or context.is_offline_mode()]
    if not present:
        return
    with op.batch_alter_table("store_submissions") as batch_op:
        for name in present:
            batch_op.drop_column(name)
