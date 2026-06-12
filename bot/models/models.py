from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey,
    Boolean, BigInteger, Float, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "tg_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_number = Column(Integer, unique=True, nullable=False)
    source_text = Column(Text, nullable=False)
    client_name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    message_id = Column(BigInteger, nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    bot_message_id = Column(BigInteger, nullable=True)

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "tg_order_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("tg_orders.id"), nullable=False)
    raw_name = Column(String(255), nullable=False)
    normalized_name = Column(String(255), nullable=True)
    quantity = Column(String(50), nullable=False)
    unit = Column(String(100), nullable=True)
    is_unknown = Column(Boolean, default=False, nullable=False)
    stock_out = Column(Boolean, default=False, nullable=False, server_default="0")

    order = relationship("Order", back_populates="items")


class Synonym(Base):
    __tablename__ = "tg_synonyms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    raw_name = Column(String(255), unique=True, nullable=False)
    normalized_name = Column(String(255), nullable=False)


class ClientNameCache(Base):
    __tablename__ = "tg_client_name_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(255), nullable=False)
    base_name = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    resolved_by = Column(String(50), nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("client_id", "base_name", name="uq_client_base"),)


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(512), nullable=False, unique=True)


class UnknownItem(Base):
    __tablename__ = "tg_unknown_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(255), nullable=False)
    order_id = Column(Integer, ForeignKey("tg_orders.id"), nullable=False)
    raw_name = Column(String(255), nullable=False)
    base_name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved = Column(Boolean, default=False, nullable=False)

    order = relationship("Order", backref="unknown_items")
