import uuid
from sqlalchemy import Column, String, Text, Float, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base
from pgvector.sqlalchemy import Vector

Base = declarative_base()

class Supplier(Base):
    __tablename__ = 'suppliers'

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, unique=True, nullable=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=True)
    address_1 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    country = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    contacts = Column(JSONB, nullable=True)

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
    __table_args__ = (
        UniqueConstraint('part_number', 'brand', name='uq_product_part_brand'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    netsuite_id = Column(String, nullable=True)
    part_number = Column(String, index=True, nullable=False)
    supplier_code = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    brand = Column(String, nullable=True)
    weight_kg = Column(Float, nullable=True)
    length_m = Column(Float, nullable=True)
    product_type = Column(String, nullable=True)
    
    # 256 dimensions for Gemini embedding-2-preview
    embedding = Column(Vector(256), nullable=True)

    def __repr__(self):
        return f"<Product(part_number='{self.part_number}', brand='{self.brand}')>"
