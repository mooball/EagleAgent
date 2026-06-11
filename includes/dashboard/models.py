import uuid
from sqlalchemy import Column, Integer, String, Text, Float, Date, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base, relationship
from pgvector.sqlalchemy import Vector

Base = declarative_base()

class Supplier(Base):
    __tablename__ = 'suppliers'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=True)
    address_1 = Column(String, nullable=True)
    address_2 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True)
    postcode = Column(String, nullable=True)
    country = Column(String, nullable=True)
    hubspot_id = Column(String, unique=True, nullable=True)
    notes = Column(Text, nullable=True)
    notes_updated_at = Column(DateTime(timezone=True), nullable=True)
    contacts = Column(JSONB, nullable=True)
    comments = Column(JSONB, nullable=True)                    # [{author, comment, ts}]
    supply_chain_position = Column(JSONB, nullable=True)       # {category, tier, confidence, reasoning}
    terms = Column(String, nullable=True)                      # e.g. "30 days", "COD"
    currency = Column(String, nullable=True)                    # e.g. "AUD", "USD", "EUR"
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
    modified_at = Column(DateTime(timezone=True), nullable=True)
    modified_by = Column(String, nullable=True)                # "user:tom", "netsuite", "ai:categorizer"
    source = Column(String(20), nullable=True, default='netsuite')  # 'netsuite' | 'web' | 'manual'
    alt_names = Column(JSONB, nullable=True)                   # ["Variant Name 1", "Variant 2"]
    alt_domains = Column(JSONB, nullable=True)                 # ["example.com.au", "example.au"]

    # 256 dimensions for Gemini embedding-2-preview (notes only)
    embedding = Column(Vector(256), nullable=True)

    # Relationships
    supplier_contacts = relationship("Contact", back_populates="supplier", foreign_keys="Contact.supplier_id")

    def __repr__(self):
        return f"<Supplier(name='{self.name}', netsuite_id='{self.netsuite_id}')>"


class SupplierBrand(Base):
    __tablename__ = 'supplier_brands'
    __table_args__ = (
        UniqueConstraint('supplier_id', 'brand_id', name='uq_supplier_brand'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey('suppliers.id'), nullable=False, index=True)
    brand_id = Column(UUID(as_uuid=True), ForeignKey('brands.id'), nullable=False, index=True)

    def __repr__(self):
        return f"<SupplierBrand(supplier_id='{self.supplier_id}', brand_id='{self.brand_id}')>"


class Brand(Base):
    __tablename__ = 'brands'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    duplicate_of = Column(UUID(as_uuid=True), ForeignKey('brands.id'), nullable=True, index=True)

    def __repr__(self):
        return f"<Brand(name='{self.name}', netsuite_id='{self.netsuite_id}')>"


class Product(Base):
    __tablename__ = 'products'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=True)
    part_number = Column(String, index=True, nullable=False)
    supplier_code = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    brand = Column(String, nullable=True)
    brand_id = Column(UUID(as_uuid=True), ForeignKey('brands.id'), nullable=True, index=True)
    weight_kg = Column(Float, nullable=True)
    length_m = Column(Float, nullable=True)
    product_type = Column(String, nullable=True)
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
    
    # 256 dimensions for Gemini embedding-2-preview
    embedding = Column(Vector(256), nullable=True)

    def __repr__(self):
        return f"<Product(part_number='{self.part_number}', brand='{self.brand}')>"


class Transaction(Base):
    """Transaction line items (Sales Orders, Quotes, legacy Purchase Orders)."""
    __tablename__ = 'product_suppliers'  # Legacy table name retained for backwards compatibility

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_number = Column(String, nullable=False, index=True)
    doc_type = Column(String, nullable=True, index=True)
    netsuite_id = Column(String, unique=True, nullable=True)
    date = Column(Date, nullable=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey('products.id'), nullable=False, index=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey('suppliers.id'), nullable=False, index=True)
    quantity = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    cost = Column(Float, nullable=True)
    cost_currency = Column(String(3), nullable=True)
    status = Column(String, nullable=True)
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)

    # Opportunity link
    netsuite_opportunity_id = Column(String, nullable=True, index=True)
    opportunity_id = Column(UUID(as_uuid=True), ForeignKey('opportunities.id'), nullable=True, index=True)

    # Relationships
    opportunity = relationship("Opportunity", back_populates="transactions", foreign_keys=[opportunity_id])

    def __repr__(self):
        return f"<Transaction(doc_number='{self.doc_number}', product_id='{self.product_id}', supplier_id='{self.supplier_id}')>"


# Backwards-compatible alias
ProductSupplier = Transaction


class Opportunity(Base):
    """NetSuite Opportunity records."""
    __tablename__ = 'opportunities'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=False, index=True)
    opportunity_number = Column(String, nullable=True)
    title = Column(String, nullable=True)
    status = Column(String, nullable=True)
    total = Column(Float, nullable=True)
    currency = Column(String, nullable=True)
    
    # Customer link
    netsuite_customer_id = Column(String, nullable=True, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=True, index=True)
    
    # Sales rep link
    netsuite_salesrep_id = Column(String, nullable=True)
    salesrep_id = Column(Integer, ForeignKey('netsuite_employee_mappings.id'), nullable=True)
    
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    customer = relationship("Customer", back_populates="opportunities", foreign_keys=[customer_id])
    salesrep = relationship("NetSuiteEmployeeMapping", foreign_keys=[salesrep_id])
    transactions = relationship("Transaction", back_populates="opportunity", foreign_keys="[Transaction.opportunity_id]")

    def __repr__(self):
        return f"<Opportunity(netsuite_id='{self.netsuite_id}', title='{self.title}')>"


class Customer(Base):
    """NetSuite Customer records."""
    __tablename__ = 'customers'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=False, index=True)
    entity_code = Column(String, nullable=True)
    companyname = Column(String, nullable=True)
    fullname = Column(String, nullable=True)
    email = Column(String, nullable=True, index=True)
    phone = Column(String, nullable=True)
    isinactive = Column(Boolean, nullable=False, default=False)
    currency = Column(String, nullable=True)
    
    # Sales rep link
    netsuite_salesrep_id = Column(String, nullable=True)
    salesrep_id = Column(Integer, ForeignKey('netsuite_employee_mappings.id'), nullable=True)
    
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    customer_contacts = relationship("Contact", back_populates="customer", foreign_keys="Contact.customer_id")
    opportunities = relationship("Opportunity", back_populates="customer", foreign_keys="Opportunity.customer_id")
    salesrep = relationship("NetSuiteEmployeeMapping", foreign_keys=[salesrep_id])

    def __repr__(self):
        return f"<Customer(netsuite_id='{self.netsuite_id}', companyname='{self.companyname}')>"


class Contact(Base):
    """Unified contact table for both suppliers and customers."""
    __tablename__ = 'contacts'
    __table_args__ = (
        # Ensure exactly one of supplier_id or customer_id is set
        # This is enforced at application level since PostgreSQL doesn't have a built-in for this
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=True, index=True)
    
    # Parent link (exactly one must be set)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey('suppliers.id'), nullable=True, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=True, index=True)
    
    label = Column(String, nullable=True)  # "Main", "Source", "Source CC" for suppliers
    firstname = Column(String, nullable=True)
    lastname = Column(String, nullable=True)
    fullname = Column(String, nullable=True)
    email = Column(String, nullable=True, index=True)
    phone = Column(String, nullable=True)
    isinactive = Column(Boolean, nullable=False, default=False)
    
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    supplier = relationship("Supplier", back_populates="supplier_contacts", foreign_keys=[supplier_id])
    customer = relationship("Customer", back_populates="customer_contacts", foreign_keys=[customer_id])

    def __repr__(self):
        if self.supplier_id:
            return f"<Contact(supplier_id='{self.supplier_id}', email='{self.email}')>"
        else:
            return f"<Contact(customer_id='{self.customer_id}', email='{self.email}')>"


class NetSuiteEmployeeMapping(Base):
    """Manual mapping of NetSuite employees to local users."""
    __tablename__ = 'netsuite_employee_mappings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    netsuite_employee_id = Column(String, unique=True, nullable=False)
    name = Column(String, nullable=False)
    email = Column(String, nullable=True, index=True)
    is_active = Column(Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<NetSuiteEmployeeMapping(netsuite_employee_id='{self.netsuite_employee_id}', name='{self.name}')>"


class RFQ(Base):
    __tablename__ = 'rfqs'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rfq_number = Column(String, unique=True, nullable=False, index=True)
    customer = Column(String, nullable=False)
    customer_contact = Column(JSONB, nullable=True)           # {name, email, phone}
    reference = Column(String, nullable=True)
    netsuite_opportunity = Column(String, nullable=True)
    hubspot_deal = Column(String, nullable=True)
    created_by = Column(String, nullable=False)
    created_date = Column(Date, nullable=False)
    assigned_to = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default='draft')  # draft/in_progress/awaiting_quotes/completed/cancelled
    notes = Column(Text, nullable=True)
    history = Column(JSONB, nullable=True)                    # [{date, user, action}, ...]
    item_groups = Column(JSONB, nullable=True)                # {groups: [...], ungrouped: [...]}
    
    # Gmail email tracking fields (summary only, email_tracking table is source of truth)
    email_status = Column(String, nullable=True)              # 'no_email_sent' | 'draft_pending' | 'sent' | 'awaiting_reply'
    last_email_sent_at = Column(DateTime(timezone=True), nullable=True)  # Most recent send time
    supplier_emails = Column(JSONB, nullable=True)            # [{email, name}, ...] contact list for multi-supplier RFQs
    
    updated_at = Column(DateTime(timezone=True), nullable=True)

    items = relationship('RFQItem', back_populates='rfq', order_by='RFQItem.line',
                         cascade='all, delete-orphan', lazy='selectin')

    def __repr__(self):
        return f"<RFQ(rfq_number='{self.rfq_number}', customer='{self.customer}')>"


class RFQThread(Base):
    """Junction table: one chat thread per user per RFQ."""
    __tablename__ = 'rfq_threads'
    __table_args__ = (
        UniqueConstraint('rfq_number', 'user_email', name='uq_rfq_thread_user'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rfq_number = Column(String, nullable=False, index=True)
    user_email = Column(String, nullable=False)
    thread_id = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<RFQThread(rfq_number='{self.rfq_number}', user='{self.user_email}')>"


class RFQItem(Base):
    __tablename__ = 'rfq_items'
    __table_args__ = (
        UniqueConstraint('rfq_id', 'line', name='uq_rfq_item_line'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey('rfqs.id'), nullable=False, index=True)
    line = Column(Integer, nullable=False)
    input_description = Column(Text, nullable=True)
    input_code = Column(String, nullable=True)
    part_number = Column(String, nullable=True)
    brand = Column(String, nullable=True)
    product_id = Column(UUID(as_uuid=True), ForeignKey('products.id'), nullable=True)
    quantity = Column(Integer, nullable=True)
    uom = Column(String, nullable=True, default='ea')
    status = Column(String, nullable=True, default='unidentified')  # unidentified/identified/confirmed/review
    notes = Column(Text, nullable=True)
    suppliers = Column(JSONB, nullable=True)                  # [{name, supplier_id, price, ...}, ...]
    brand_suppliers = Column(JSONB, nullable=True)            # [{name, supplier_id, tier, transaction_count, ...}] — non-Tier-A overflow

    rfq = relationship('RFQ', back_populates='items')

    def __repr__(self):
        return f"<RFQItem(rfq_id='{self.rfq_id}', line={self.line})>"


class MailboxScanConfig(Base):
    """Admin config for which mailboxes to include in Gmail scanning."""
    __tablename__ = 'mailbox_scan_config'

    user_email = Column(String, primary_key=True)
    scan_enabled = Column(Boolean, nullable=False, default=True)
    excluded_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self):
        return f"<MailboxScanConfig(user_email='{self.user_email}', scan_enabled={self.scan_enabled})>"


class EmailTracking(Base):
    """Tracks all email lifecycle events (draft, sent, received)."""
    __tablename__ = 'email_tracking'

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Gmail identifiers
    gmail_thread_id = Column(String, nullable=False, index=True)
    gmail_message_id = Column(String, nullable=True, unique=True)
    gmail_draft_id = Column(String, nullable=True, unique=True)
    gmail_history_id = Column(Integer, nullable=True)
    gmail_label = Column(String, nullable=True, default='agent-rfq')

    # User & context
    user_email = Column(String, nullable=False, index=True)

    # RFQ/Opportunity tracking (nullable for contact-matched emails)
    rfq_id = Column(String, nullable=True, index=True)
    opportunity_id = Column(String, nullable=True, index=True)
    rfq_token = Column(String, nullable=True)

    # Entity linking (from Tier 3 contact/domain matching)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey('suppliers.id'), nullable=True, index=True)
    customer_id = Column(UUID(as_uuid=True), ForeignKey('customers.id'), nullable=True, index=True)
    match_type = Column(String, nullable=True)  # 'exact' | 'domain' | NULL (ID/subject matched)

    # Email metadata
    direction = Column(String, nullable=False)   # 'draft' | 'sent' | 'received'
    email_type = Column(String, nullable=True)   # 'rfq_outreach' | 'quote' | 'invoice' | etc.
    subject = Column(String, nullable=True)
    recipient_email = Column(String, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    # Workflow state
    draft_url = Column(String, nullable=True)
    draft_opened_at = Column(DateTime(timezone=True), nullable=True)
    sent_confirmed = Column(Boolean, nullable=True, default=False)

    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    supplier = relationship("Supplier", foreign_keys=[supplier_id])
    customer = relationship("Customer", foreign_keys=[customer_id])

    def __repr__(self):
        return f"<EmailTracking(id={self.id}, direction='{self.direction}', rfq_id='{self.rfq_id}')>"

