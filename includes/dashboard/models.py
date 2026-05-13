import uuid
from sqlalchemy import Column, Integer, String, Text, Float, Date, DateTime, ForeignKey, UniqueConstraint
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
    contacts = Column(JSONB, nullable=True)
    comments = Column(JSONB, nullable=True)                    # [{author, comment, ts}]
    supply_chain_position = Column(JSONB, nullable=True)       # {category, tier, confidence, reasoning}
    terms = Column(String, nullable=True)                      # e.g. "30 days", "COD"
    currency = Column(String, nullable=True)                    # e.g. "AUD", "USD", "EUR"
    netsuite_last_modified = Column(DateTime(timezone=True), nullable=True)
    modified_at = Column(DateTime(timezone=True), nullable=True)
    modified_by = Column(String, nullable=True)                # "user:tom", "netsuite", "ai:categorizer"
    source = Column(String(20), nullable=True, default='netsuite')  # 'netsuite' | 'web' | 'manual'

    # 256 dimensions for Gemini embedding-2-preview (notes only)
    embedding = Column(Vector(256), nullable=True)

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

    def __repr__(self):
        return f"<Transaction(doc_number='{self.doc_number}', product_id='{self.product_id}', supplier_id='{self.supplier_id}')>"


# Backwards-compatible alias
ProductSupplier = Transaction


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

    rfq = relationship('RFQ', back_populates='items')

    def __repr__(self):
        return f"<RFQItem(rfq_id='{self.rfq_id}', line={self.line})>"
