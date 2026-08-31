"""add audit_log

Revision ID: a7c3e9d21f04
Revises: d3f7a91c4b06
Create Date: 2026-08-21 10:12:44.301887

The application-level access log required by 164.312(b). Infrastructure logs
cannot satisfy it: they record that a request happened, not which client's
record a named user opened.

Note there is deliberately NO foreign key on actor_user_id. Rejecting a signup
hard-deletes the user account (/api/auth/role-requests/{id}), and a FK would
either cascade that deletion into this table — erasing the history at the
moment it matters most — or null the actor out. The id is stored bare with the
role and display name snapshot beside it, so a record outlives its user.

entity_ids is intentionally left unindexed; see the model for why.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7c3e9d21f04'
down_revision: Union[str, None] = 'd3f7a91c4b06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'audit_log',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('actor_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('actor_role', sa.String(), nullable=True),
        sa.Column('actor_label', sa.String(), nullable=True),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('entity_type', sa.String(), nullable=False),
        sa.Column('entity_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('entity_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('entity_count', sa.Integer(), nullable=True),
        sa.Column('truncated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('changed_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('criteria', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('source_ip', sa.String(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('route', sa.String(), nullable=True),
        sa.Column('request_id', sa.String(), nullable=True),
        sa.Column('outcome', sa.String(), nullable=False, server_default='success'),
        sa.Column('status_code', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_audit_log_created_at', 'audit_log', ['created_at'])
    op.create_index('ix_audit_log_actor', 'audit_log', ['actor_user_id', 'created_at'])
    op.create_index('ix_audit_log_entity', 'audit_log', ['entity_type', 'entity_id', 'created_at'])
    op.create_index('ix_audit_log_request', 'audit_log', ['request_id'])


def downgrade() -> None:
    op.drop_index('ix_audit_log_request', table_name='audit_log')
    op.drop_index('ix_audit_log_entity', table_name='audit_log')
    op.drop_index('ix_audit_log_actor', table_name='audit_log')
    op.drop_index('ix_audit_log_created_at', table_name='audit_log')
    op.drop_table('audit_log')
