"""add enrollments table and convert payments to a ledger

Revision ID: 4b8f485d5e40
Revises: 6051f9682593
Create Date: 2026-08-05 16:04:15.211702

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4b8f485d5e40'
down_revision: Union[str, None] = '6051f9682593'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. New enrollments table.
    op.create_table(
        'enrollments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('client_id', sa.UUID(), nullable=False),
        sa.Column('package_id', sa.UUID(), nullable=False),
        sa.Column('package_price_snapshot', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('total_paid', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('amount_due', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column('status', sa.Enum('active', 'completed', name='enrollment_status'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id']),
        sa.ForeignKeyConstraint(['package_id'], ['packages.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_enrollments_client_package', 'enrollments', ['client_id', 'package_id'])
    # Enforces "at most one active enrollment per client+package" — completing
    # one and starting a new cycle always means a fresh row, never reopening
    # the old one.
    op.create_index(
        'uq_active_enrollment', 'enrollments', ['client_id', 'package_id'],
        unique=True, postgresql_where=sa.text("status = 'active'"),
    )

    # 2. Backfill: one enrollment per distinct (client_id, package_id) pair
    # that has existing payment rows. Pre-migration data has no cycle marker,
    # so this collapses each pair's whole history into a single enrollment —
    # a known, documented simplification for pre-existing data only; every
    # enrollment created going forward is a real, separate cycle.
    op.execute("""
        INSERT INTO enrollments
            (id, client_id, package_id, package_price_snapshot, total_paid,
             amount_due, status, started_at, completed_at, created_at, updated_at)
        SELECT
            gen_random_uuid(),
            p.client_id,
            p.package_id,
            MAX(p.due) AS package_price_snapshot,
            SUM(p.paid) AS total_paid,
            GREATEST(MAX(p.due) - SUM(p.paid), 0) AS amount_due,
            (CASE WHEN SUM(p.paid) >= MAX(p.due) THEN 'completed' ELSE 'active' END)::enrollment_status,
            MIN(p.date)::timestamptz AS started_at,
            CASE WHEN SUM(p.paid) >= MAX(p.due) THEN MAX(p.date)::timestamptz ELSE NULL END,
            now(),
            now()
        FROM payments p
        GROUP BY p.client_id, p.package_id
    """)

    # 3. Extend payments: enrollment_id (nullable until backfilled), the
    # renamed/new ledger columns.
    op.add_column('payments', sa.Column('enrollment_id', sa.UUID(), nullable=True))
    op.add_column('payments', sa.Column('balance_after', sa.Numeric(precision=10, scale=2), nullable=True))
    op.add_column('payments', sa.Column('created_by', sa.UUID(), nullable=True))

    op.execute("""
        UPDATE payments p
        SET enrollment_id = e.id,
            balance_after = GREATEST(p.due - p.paid, 0)
        FROM enrollments e
        WHERE e.client_id = p.client_id AND e.package_id = p.package_id
    """)

    op.alter_column('payments', 'enrollment_id', nullable=False)
    op.alter_column('payments', 'balance_after', nullable=False)

    # paid -> amount_paid is a rename, not a drop+add, so existing values
    # survive untouched.
    op.alter_column('payments', 'paid', new_column_name='amount_paid')

    op.create_foreign_key(
        'fk_payments_enrollment_id', 'payments', 'enrollments', ['enrollment_id'], ['id']
    )
    op.create_foreign_key(
        'fk_payments_created_by', 'payments', 'users', ['created_by'], ['id']
    )
    op.create_index('ix_payments_enrollment_date', 'payments', ['enrollment_id', 'date'])
    op.create_index('ix_payments_client_package', 'payments', ['client_id', 'package_id'])

    # 4. Drop what a ledger row no longer needs: due/status now live on the
    # enrollment, not the individual payment.
    op.drop_column('payments', 'due')
    op.drop_column('payments', 'status')
    op.execute('DROP TYPE IF EXISTS payment_status')

    # NOTE: autogenerate also proposed `op.drop_table('integrations')` here,
    # since that model was removed from the codebase separately. Left out —
    # dropping a table is destructive and wasn't asked for in this change.


def downgrade() -> None:
    op.add_column('payments', sa.Column(
        'status', sa.Enum('paid', 'partially_paid', 'pending', 'overdue', name='payment_status'),
        nullable=False, server_default='pending',
    ))
    op.add_column('payments', sa.Column('due', sa.Numeric(precision=10, scale=2), nullable=True))
    op.execute('UPDATE payments SET due = amount_paid + balance_after')
    op.alter_column('payments', 'due', nullable=False)

    op.drop_index('ix_payments_client_package', table_name='payments')
    op.drop_index('ix_payments_enrollment_date', table_name='payments')
    op.drop_constraint('fk_payments_created_by', 'payments', type_='foreignkey')
    op.drop_constraint('fk_payments_enrollment_id', 'payments', type_='foreignkey')

    op.alter_column('payments', 'amount_paid', new_column_name='paid')

    op.drop_column('payments', 'created_by')
    op.drop_column('payments', 'balance_after')
    op.drop_column('payments', 'enrollment_id')

    op.drop_index('uq_active_enrollment', table_name='enrollments')
    op.drop_index('ix_enrollments_client_package', table_name='enrollments')
    op.drop_table('enrollments')
    op.execute('DROP TYPE IF EXISTS enrollment_status')
