"""Alter scan_sessions created_at and price_entries timestamp to TIMESTAMPTZ

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-14 11:24:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use alter column to TIMESTAMP WITH TIME ZONE if postgresql
    bind = op.get_bind()
    if bind.engine.name == 'postgresql':
        op.execute("ALTER TABLE scan_sessions ALTER COLUMN created_at TYPE TIMESTAMP WITH TIME ZONE USING created_at AT TIME ZONE 'UTC';")
        op.execute("ALTER TABLE price_entries ALTER COLUMN timestamp TYPE TIMESTAMP WITH TIME ZONE USING timestamp AT TIME ZONE 'UTC';")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.engine.name == 'postgresql':
        op.execute("ALTER TABLE scan_sessions ALTER COLUMN created_at TYPE TIMESTAMP WITHOUT TIME ZONE;")
        op.execute("ALTER TABLE price_entries ALTER COLUMN timestamp TYPE TIMESTAMP WITHOUT TIME ZONE;")
