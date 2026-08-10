from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, index=True)
    nama = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    reputasi_score = Column(Integer, default=0)

class Store(Base):
    __tablename__ = "stores"
    
    id = Column(String, primary_key=True, index=True)
    nama = Column(String, nullable=False)
    alamat = Column(String, nullable=True)
    lat = Column(Float, nullable=True)
    lng = Column(Float, nullable=True)

class Product(Base):
    __tablename__ = "products"
    
    id = Column(String, primary_key=True, index=True)
    nama = Column(String, unique=True, index=True, nullable=False)
    kategori = Column(String, default="General", nullable=False)
    ukuran = Column(Float, nullable=True)
    satuan = Column(String, nullable=True)
    
    # Relationship to price entries
    price_entries = relationship("PriceEntry", back_populates="product", cascade="all, delete-orphan")

class PriceEntry(Base):
    __tablename__ = "price_entries"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    product_id = Column(String, ForeignKey("products.id"), nullable=False)
    store_id = Column(String, nullable=False)
    harga = Column(Integer, nullable=False)
    sumber_user_id = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    status_verifikasi = Column(String, default="pending", nullable=False)
    
    # Relationship to product
    product = relationship("Product", back_populates="price_entries")
