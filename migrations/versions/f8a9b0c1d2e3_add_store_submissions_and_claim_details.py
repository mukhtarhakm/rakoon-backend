"""Add store submissions and claim details

Revision ID: f8a9b0c1d2e3
Revises: e7a8b9c0d1e2
Create Date: 2026-10-13 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op, context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'e7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    # 1. Add claim detail columns to store_owners
    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.add_column(sa.Column("applicant_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("applicant_phone", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("applicant_role", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("document_url", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("store_photo_url", sa.String(), nullable=True))

    # 2. Create store_submissions table
    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'store_submissions' in tables:
            return

    op.create_table(
        'store_submissions',
        sa.Column('id', sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column('user_id', sa.Uuid(as_uuid=False), nullable=False),
        sa.Column('store_name', sa.String(), nullable=False),
        sa.Column('business_entity_name', sa.String(), nullable=True),
        sa.Column('store_address', sa.String(), nullable=False),
        sa.Column('store_category', sa.String(), nullable=True, server_default='Supermarket'),
        sa.Column('applicant_name', sa.String(), nullable=False),
        sa.Column('applicant_phone', sa.String(), nullable=False),
        sa.Column('applicant_role', sa.String(), nullable=False),
        sa.Column('document_url', sa.String(), nullable=True),
        sa.Column('store_photo_url', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('rejection_reason', sa.String(), nullable=True),
        sa.Column('created_store_id', sa.Uuid(as_uuid=False), sa.ForeignKey('stores.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by', sa.String(), nullable=True),
    )
    op.create_index('ix_store_submissions_id', 'store_submissions', ['id'], unique=False)
    op.create_index('ix_store_submissions_user_id', 'store_submissions', ['user_id'], unique=False)
    op.create_index('ix_store_submissions_status', 'store_submissions', ['status'], unique=False)


def downgrade() -> None:
    is_offline = context.is_offline_mode()
    bind = op.get_bind()

    if not is_offline:
        inspector = sa.inspect(bind)
        tables = inspector.get_table_names()
        if 'store_submissions' in tables:
            op.drop_index('ix_store_submissions_status', table_name='store_submissions')
            op.drop_index('ix_store_submissions_user_id', table_name='store_submissions')
            op.drop_index('ix_store_submissions_id', table_name='store_submissions')
            op.drop_table('store_submissions')
    else:
        op.drop_index('ix_store_submissions_status', table_name='store_submissions')
        op.drop_index('ix_store_submissions_user_id', table_name='store_submissions')
        op.drop_index('ix_store_submissions_id', table_name='store_submissions')
        op.drop_table('store_submissions')

    with op.batch_alter_table("store_owners") as batch_op:
        batch_op.drop_column("store_photo_url")
        batch_op.drop_column("document_url")
        batch_op.drop_column("applicant_role")
        batch_op.drop_column("applicant_phone")
        batch_op.drop_column("applicant_name")
