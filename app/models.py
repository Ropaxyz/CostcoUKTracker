"""
Database models for Costco Tracker.
Uses SQLAlchemy with SQLite (or PostgreSQL).
"""

from datetime import datetime
from typing import Optional
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    Text, ForeignKey, Enum as SQLEnum, Index, UniqueConstraint
)
from sqlalchemy.orm import relationship, declarative_base
from enum import Enum

Base = declarative_base()


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    WAREHOUSE_ONLY = "warehouse_only"
    UNKNOWN = "unknown"
    REMOVED = "removed"


class AlertType(str, Enum):
    BACK_IN_STOCK = "back_in_stock"
    PRICE_DROP = "price_drop"
    TARGET_PRICE = "target_price"
    LOWEST_EVER = "lowest_ever"
    STOCK_FLAPPING = "stock_flapping"
    ADDED_TO_BASKET = "added_to_basket"


class NotificationChannel(str, Enum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    PUSHOVER = "pushover"


class Product(Base):
    """Tracked product from Costco UK."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_number = Column(String(50), unique=True, nullable=False, index=True)
    url = Column(String(500), nullable=False)
    name = Column(String(500), nullable=True)
    image_url = Column(String(500), nullable=True)

    # Current state
    current_price = Column(Float, nullable=True)
    previous_price = Column(Float, nullable=True)
    lowest_price = Column(Float, nullable=True)
    highest_price = Column(Float, nullable=True)
    stock_status = Column(String(50), default=StockStatus.UNKNOWN.value)

    # Checkout discount (e.g., "£300 off at checkout")
    checkout_discount = Column(Float, nullable=True)
    checkout_discount_text = Column(String(500), nullable=True)

    # Tracking settings
    is_active = Column(Boolean, default=True)
    poll_interval_minutes = Column(Integer, nullable=True)  # Override default
    target_price = Column(Float, nullable=True)

    # Assisted checkout
    auto_add_to_basket = Column(Boolean, default=False)
    auto_add_quantity = Column(Integer, default=1)
    auto_add_max_price = Column(Float, nullable=True)  # Only add if below this price

    # Notifications
    notify_back_in_stock = Column(Boolean, default=True)
    notify_price_drop = Column(Boolean, default=True)
    notify_target_price = Column(Boolean, default=True)
    notify_lowest_ever = Column(Boolean, default=True)
    notification_channels = Column(String(200), default="email,telegram,discord,pushover")

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_checked_at = Column(DateTime, nullable=True)
    last_in_stock_at = Column(DateTime, nullable=True)
    last_price_change_at = Column(DateTime, nullable=True)

    # Error tracking
    consecutive_errors = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)

    # Relationships
    price_history = relationship("PriceHistory", back_populates="product", cascade="all, delete-orphan")
    stock_history = relationship("StockHistory", back_populates="product", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="product", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Product {self.item_number}: {self.name}>"

    @property
    def price_change_percent(self) -> Optional[float]:
        if self.current_price and self.previous_price and self.previous_price > 0:
            return ((self.current_price - self.previous_price) / self.previous_price) * 100
        return None

    @property
    def effective_price(self) -> Optional[float]:
        """Calculate the actual price after checkout discount."""
        if self.current_price is None:
            return None
        if self.checkout_discount:
            return max(0, self.current_price - self.checkout_discount)
        return self.current_price

    @property
    def is_clearance_price(self) -> bool:
        """Detect .97 or .00 clearance pricing."""
        if self.current_price:
            cents = int(round(self.current_price * 100)) % 100
            return cents in [97, 0, 88, 49]
        return False

    @property
    def enabled_channels(self) -> list[str]:
        return [ch.strip() for ch in self.notification_channels.split(",") if ch.strip()]


class PriceHistory(Base):
    """Historical price records for a product."""
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float, nullable=False)
    previous_price = Column(Float, nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)

    product = relationship("Product", back_populates="price_history")

    __table_args__ = (
        Index("idx_price_history_product_date", "product_id", "recorded_at"),
    )


class StockHistory(Base):
    """Historical stock status records for a product."""
    __tablename__ = "stock_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    status = Column(String(50), nullable=False)
    previous_status = Column(String(50), nullable=True)
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)

    product = relationship("Product", back_populates="stock_history")

    __table_args__ = (
        Index("idx_stock_history_product_date", "product_id", "recorded_at"),
    )


class Alert(Base):
    """Alert/notification records."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    alert_type = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)

    old_value = Column(String(100), nullable=True)
    new_value = Column(String(100), nullable=True)

    sent_at = Column(DateTime, default=datetime.utcnow)
    channels_sent = Column(String(200), nullable=True)  # Comma-separated

    product = relationship("Product", back_populates="alerts")

    __table_args__ = (
        Index("idx_alerts_product_date", "product_id", "sent_at"),
    )


class SystemSettings(Base):
    """System-wide settings stored in database."""
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SchedulerStatus(Base):
    """Scheduler run history and status."""
    __tablename__ = "scheduler_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_started_at = Column(DateTime, default=datetime.utcnow)
    run_completed_at = Column(DateTime, nullable=True)
    products_checked = Column(Integer, default=0)
    products_updated = Column(Integer, default=0)
    errors_count = Column(Integer, default=0)
    status = Column(String(50), default="running")
    details = Column(Text, nullable=True)


class BasketAction(Base):
    """Log of auto-add-to-basket actions."""
    __tablename__ = "basket_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    action = Column(String(50), nullable=False)  # attempted, success, failed
    price_at_action = Column(Float, nullable=True)
    quantity = Column(Integer, default=1)
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_basket_actions_product_date", "product_id", "created_at"),
    )
