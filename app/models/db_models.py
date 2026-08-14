from datetime import datetime, timezone
import uuid
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"
    
    id = Column(Uuid(as_uuid=False), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    nama = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    reputasi_score = Column(Integer, default=0)

class Store(Base):
    __tablename__ = "stores"
    
    id = Column(Uuid(as_uuid=False), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    nama = Column(String, nullable=False)
    alamat = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)


class Product(Base):
    __tablename__ = "products"
    
    id = Column(Uuid(as_uuid=False), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    nama = Column(String, unique=True, index=True, nullable=False)
    kategori = Column(String, default="General", nullable=False)
    ukuran = Column(Float, nullable=True)
    satuan = Column(String, nullable=True)
    
    # Relationship to price entries
    price_entries = relationship("PriceEntry", back_populates="product", cascade="all, delete-orphan")

class ScanSession(Base):
    __tablename__ = "scan_sessions"
    
    id = Column(Uuid(as_uuid=False), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(Uuid(as_uuid=False), nullable=False, index=True)
    store_id = Column(Uuid(as_uuid=False), ForeignKey("stores.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    
    # Relationships
    store = relationship("Store")
    price_entries = relationship("PriceEntry", back_populates="scan_session", cascade="all, delete-orphan")

class PriceEntry(Base):
    __tablename__ = "price_entries"
    
    id = Column(Uuid(as_uuid=False), primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    product_id = Column(Uuid(as_uuid=False), ForeignKey("products.id"), nullable=False)
    store_id = Column(Uuid(as_uuid=False), nullable=False)
    harga = Column(Integer, nullable=False)
    sumber_user_id = Column(Uuid(as_uuid=False), nullable=False)
    timestamp = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    status_verifikasi = Column(String, default="pending", nullable=False)
    scan_session_id = Column(Uuid(as_uuid=False), ForeignKey("scan_sessions.id"), nullable=True, index=True)
    
    # Relationships
    product = relationship("Product", back_populates="price_entries")
    scan_session = relationship("ScanSession", back_populates="price_entries")
