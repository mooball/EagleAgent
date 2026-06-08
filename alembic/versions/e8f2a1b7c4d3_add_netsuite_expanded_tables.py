"""Add NetSuite expanded tables: opportunities, customers, contacts, employee mappings.

Revision ID: e8f2a1b7c4d3
Revises: c3d4e5f6a7b8
Create Date: 2026-06-08 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid

# revision identifiers, used by Alembic.
revision = 'e8f2a1b7c4d3'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create netsuite_employee_mappings table first (no dependencies)
    op.create_table(
        'netsuite_employee_mappings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('netsuite_employee_id', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_employee_id', name='uq_netsuite_employee_id'),
    )
    op.create_index('ix_netsuite_employee_mappings_email', 'netsuite_employee_mappings', ['email'])

    # Create customers table
    op.create_table(
        'customers',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, default=lambda: str(uuid.uuid4())),
        sa.Column('netsuite_id', sa.String(), nullable=False),
        sa.Column('entity_code', sa.String(), nullable=True),
        sa.Column('companyname', sa.String(), nullable=False),
        sa.Column('fullname', sa.String(), nullable=True),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('phone', sa.String(), nullable=True),
        sa.Column('isinactive', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('currency', sa.String(), nullable=True),
        sa.Column('netsuite_salesrep_id', sa.String(), nullable=True),
        sa.Column('salesrep_id', sa.Integer(), nullable=True),
        sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['salesrep_id'], ['netsuite_employee_mappings.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_id', name='uq_customer_netsuite_id'),
    )
    op.create_index('ix_customers_netsuite_id', 'customers', ['netsuite_id'])
    op.create_index('ix_customers_email', 'customers', ['email'])
    op.create_index('ix_customers_salesrep_id', 'customers', ['salesrep_id'])
    op.create_index('ix_customers_netsuite_last_modified', 'customers', ['netsuite_last_modified'])

    # Create opportunities table
    op.create_table(
        'opportunities',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, default=lambda: str(uuid.uuid4())),
        sa.Column('netsuite_id', sa.String(), nullable=False),
        sa.Column('opportunity_number', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=True),
        sa.Column('total', sa.Float(), nullable=True),
        sa.Column('currency', sa.String(), nullable=True),
        sa.Column('netsuite_customer_id', sa.String(), nullable=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('netsuite_salesrep_id', sa.String(), nullable=True),
        sa.Column('salesrep_id', sa.Integer(), nullable=True),
        sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.ForeignKeyConstraint(['salesrep_id'], ['netsuite_employee_mappings.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_id', name='uq_opportunity_netsuite_id'),
    )
    op.create_index('ix_opportunities_netsuite_id', 'opportunities', ['netsuite_id'])
    op.create_index('ix_opportunities_netsuite_customer_id', 'opportunities', ['netsuite_customer_id'])
    op.create_index('ix_opportunities_customer_id', 'opportunities', ['customer_id'])
    op.create_index('ix_opportunities_salesrep_id', 'opportunities', ['salesrep_id'])
    op.create_index('ix_opportunities_netsuite_last_modified', 'opportunities', ['netsuite_last_modified'])

    # Create contacts table (unified for suppliers and customers)
    op.create_table(
        'contacts',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, default=lambda: str(uuid.uuid4())),
        sa.Column('netsuite_id', sa.String(), nullable=True),
        sa.Column('supplier_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('customer_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('firstname', sa.String(), nullable=True),
        sa.Column('lastname', sa.String(), nullable=True),
        sa.Column('fullname', sa.String(), nullable=True),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('phone', sa.String(), nullable=True),
        sa.Column('isinactive', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('netsuite_last_modified', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('netsuite_id', name='uq_contact_netsuite_id'),
    )
    op.create_index('ix_contacts_netsuite_id', 'contacts', ['netsuite_id'])
    op.create_index('ix_contacts_supplier_id', 'contacts', ['supplier_id'])
    op.create_index('ix_contacts_customer_id', 'contacts', ['customer_id'])
    op.create_index('ix_contacts_email', 'contacts', ['email'])
    op.create_index('ix_contacts_netsuite_last_modified', 'contacts', ['netsuite_last_modified'])


def downgrade() -> None:
    op.drop_index('ix_contacts_netsuite_last_modified', table_name='contacts')
    op.drop_index('ix_contacts_email', table_name='contacts')
    op.drop_index('ix_contacts_customer_id', table_name='contacts')
    op.drop_index('ix_contacts_supplier_id', table_name='contacts')
    op.drop_index('ix_contacts_netsuite_id', table_name='contacts')
    op.drop_table('contacts')
    
    op.drop_index('ix_opportunities_netsuite_last_modified', table_name='opportunities')
    op.drop_index('ix_opportunities_salesrep_id', table_name='opportunities')
    op.drop_index('ix_opportunities_customer_id', table_name='opportunities')
    op.drop_index('ix_opportunities_netsuite_customer_id', table_name='opportunities')
    op.drop_index('ix_opportunities_netsuite_id', table_name='opportunities')
    op.drop_table('opportunities')
    
    op.drop_index('ix_customers_netsuite_last_modified', table_name='customers')
    op.drop_index('ix_customers_salesrep_id', table_name='customers')
    op.drop_index('ix_customers_email', table_name='customers')
    op.drop_index('ix_customers_netsuite_id', table_name='customers')
    op.drop_table('customers')
    
    op.drop_index('ix_netsuite_employee_mappings_email', table_name='netsuite_employee_mappings')
    op.drop_table('netsuite_employee_mappings')
