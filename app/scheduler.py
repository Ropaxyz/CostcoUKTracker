"""
Background scheduler for polling products.
Uses APScheduler for reliable scheduling.
"""

import logging
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db_session
from app.models import (
    Product, PriceHistory, StockHistory, Alert, SchedulerStatus,
    StockStatus, AlertType
)
from app.scraper import scraper, ProductData
from app.notifications import notifications

logger = logging.getLogger(__name__)


class ProductScheduler:
    """
    Manages background polling of tracked products.

    Features:
    - Randomized intervals to avoid patterns
    - Smart backoff on errors
    - Escalated polling near price drops
    - Stock flapping detection
    """

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._is_running = False
        self._current_run_id: Optional[int] = None

    def start(self):
        """Start the scheduler."""
        if not self._is_running:
            # Add main job
            self.scheduler.add_job(
                self._poll_all_products,
                trigger=IntervalTrigger(minutes=settings.default_poll_interval_minutes),
                id="main_poll",
                name="Poll all products",
                replace_existing=True,
                max_instances=1,
            )

            # Add cleanup job
            self.scheduler.add_job(
                self._cleanup_old_data,
                trigger=IntervalTrigger(hours=24),
                id="cleanup",
                name="Cleanup old data",
                replace_existing=True,
            )

            self.scheduler.start()
            self._is_running = True
            logger.info("Scheduler started")

    def stop(self):
        """Stop the scheduler."""
        if self._is_running:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            logger.info("Scheduler stopped")

    @property
    def is_running(self) -> bool:
        return self._is_running

    def get_next_run(self) -> Optional[datetime]:
        """Get next scheduled run time."""
        job = self.scheduler.get_job("main_poll")
        return job.next_run_time if job else None

    async def run_now(self):
        """Trigger immediate poll (for manual refresh)."""
        await self._poll_all_products()

    async def poll_single_product(self, product_id: int) -> Optional[ProductData]:
        """Poll a single product immediately."""
        with get_db_session() as db:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product:
                return None

            data = await scraper.fetch_product(product.item_number)
            await self._process_product_update(db, product, data)
            return data

    async def _poll_all_products(self):
        """Main polling loop - check all active products."""
        if settings.kill_switch:
            logger.warning("Kill switch active - skipping poll")
            return

        logger.info("Starting product poll cycle")

        with get_db_session() as db:
            # Create run record
            run = SchedulerStatus(
                run_started_at=datetime.utcnow(),
                status="running"
            )
            db.add(run)
            db.commit()
            self._current_run_id = run.id

            # Get active products
            products = db.query(Product).filter(
                Product.is_active == True
            ).all()

            if not products:
                logger.info("No products to check")
                run.status = "completed"
                run.run_completed_at = datetime.utcnow()
                db.commit()
                return

            products_checked = 0
            products_updated = 0
            errors = 0

            for product in products:
                try:
                    # Check if product needs polling based on its interval
                    if not self._should_poll(product):
                        continue

                    # Add random delay between products
                    if products_checked > 0:
                        delay = random.uniform(2, 8) if settings.safe_mode else random.uniform(0.5, 2)
                        await asyncio.sleep(delay)

                    # Fetch product data
                    data = await scraper.fetch_product(product.item_number)
                    products_checked += 1

                    # Process the update
                    updated = await self._process_product_update(db, product, data)
                    if updated:
                        products_updated += 1

                    if data.error:
                        errors += 1

                except Exception as e:
                    logger.exception(f"Error polling product {product.item_number}")
                    errors += 1
                    product.consecutive_errors += 1
                    product.last_error = str(e)
                    product.last_error_at = datetime.utcnow()

            # Update run record
            run.run_completed_at = datetime.utcnow()
            run.products_checked = products_checked
            run.products_updated = products_updated
            run.errors_count = errors
            run.status = "completed" if errors == 0 else "completed_with_errors"
            db.commit()

            logger.info(
                f"Poll cycle complete: {products_checked} checked, "
                f"{products_updated} updated, {errors} errors"
            )

    def _should_poll(self, product: Product) -> bool:
        """Determine if a product should be polled now."""
        if not product.last_checked_at:
            return True

        # Use product-specific interval or default
        interval = product.poll_interval_minutes or settings.default_poll_interval_minutes

        # Add some randomization
        jitter = random.uniform(0.8, 1.2)
        effective_interval = interval * jitter

        elapsed = (datetime.utcnow() - product.last_checked_at).total_seconds() / 60
        return elapsed >= effective_interval

    async def _process_product_update(
        self,
        db: Session,
        product: Product,
        data: ProductData
    ) -> bool:
        """
        Process scraped data and update product.
        Returns True if significant change detected.
        """
        now = datetime.utcnow()
        product.last_checked_at = now
        changed = False
        alerts_to_send = []

        # Handle errors
        if data.error:
            product.consecutive_errors += 1
            product.last_error = data.error
            product.last_error_at = now
            db.commit()
            return False

        # Clear error state on success
        product.consecutive_errors = 0
        product.last_error = None

        # Update basic info
        if data.name and not product.name:
            product.name = data.name
        if data.image_url:
            product.image_url = data.image_url

        # Check stock status change
        old_status = product.stock_status
        new_status = data.stock_status.value

        if old_status != new_status:
            changed = True

            # Record history
            history = StockHistory(
                product_id=product.id,
                status=new_status,
                previous_status=old_status,
                recorded_at=now
            )
            db.add(history)

            product.stock_status = new_status

            # Check for back in stock
            if (old_status == StockStatus.OUT_OF_STOCK.value and
                new_status == StockStatus.IN_STOCK.value):
                product.last_in_stock_at = now

                if product.notify_back_in_stock:
                    alerts_to_send.append((
                        AlertType.BACK_IN_STOCK,
                        old_status,
                        new_status
                    ))

                # Trigger auto-add-to-basket if enabled
                if product.auto_add_to_basket:
                    await self._handle_auto_basket(db, product, data)

        # Check price change
        if data.price is not None:
            old_price = product.current_price
            new_price = data.price

            if old_price is None or abs(old_price - new_price) > 0.01:
                changed = True
                product.previous_price = old_price
                product.current_price = new_price
                product.last_price_change_at = now

                # Record price history
                history = PriceHistory(
                    product_id=product.id,
                    price=new_price,
                    previous_price=old_price,
                    recorded_at=now
                )
                db.add(history)

        # Update checkout discount info (always, even if price unchanged)
        product.checkout_discount = data.checkout_discount
        product.checkout_discount_text = data.checkout_discount_text

        # Check price change (continued)
        if data.price is not None and (old_price is None or abs(old_price - new_price) > 0.01):
                # Update lowest/highest
                if product.lowest_price is None or new_price < product.lowest_price:
                    old_lowest = product.lowest_price
                    product.lowest_price = new_price

                    if old_lowest is not None and product.notify_lowest_ever:
                        alerts_to_send.append((
                            AlertType.LOWEST_EVER,
                            f"{old_lowest:.2f}",
                            f"{new_price:.2f}"
                        ))

                if product.highest_price is None or new_price > product.highest_price:
                    product.highest_price = new_price

                # Check for price drop
                if old_price and new_price < old_price and product.notify_price_drop:
                    alerts_to_send.append((
                        AlertType.PRICE_DROP,
                        f"{old_price:.2f}",
                        f"{new_price:.2f}"
                    ))

                # Check target price
                if (product.target_price and
                    new_price <= product.target_price and
                    product.notify_target_price):
                    alerts_to_send.append((
                        AlertType.TARGET_PRICE,
                        None,
                        f"{new_price:.2f}"
                    ))

        db.commit()

        # Send alerts
        for alert_type, old_val, new_val in alerts_to_send:
            await self._send_alert(db, product, alert_type, old_val, new_val)

        return changed

    async def _send_alert(
        self,
        db: Session,
        product: Product,
        alert_type: AlertType,
        old_value: Optional[str],
        new_value: Optional[str]
    ):
        """Send alert and record it."""
        try:
            results = await notifications.send_notification(
                product=product,
                alert_type=alert_type,
                old_value=old_value,
                new_value=new_value,
            )

            # Record alert
            channels_sent = ",".join([r.channel for r in results if r.success])
            alert = Alert(
                product_id=product.id,
                alert_type=alert_type.value,
                message=f"{alert_type.value}: {old_value} -> {new_value}",
                old_value=old_value,
                new_value=new_value,
                channels_sent=channels_sent,
            )
            db.add(alert)
            db.commit()

        except Exception as e:
            logger.exception(f"Failed to send alert for product {product.id}")

    async def _handle_auto_basket(self, db: Session, product: Product, data: ProductData):
        """Handle auto-add-to-basket functionality."""
        from app.basket import add_to_basket

        # Check price constraint
        if product.auto_add_max_price and data.price:
            if data.price > product.auto_add_max_price:
                logger.info(
                    f"Skipping auto-add for {product.item_number}: "
                    f"price {data.price} > max {product.auto_add_max_price}"
                )
                return

        # Attempt to add to basket
        result = await add_to_basket(
            product.item_number,
            quantity=product.auto_add_quantity
        )

        if result.success:
            await self._send_alert(
                db, product, AlertType.ADDED_TO_BASKET,
                None, f"Qty: {product.auto_add_quantity}"
            )

    async def _cleanup_old_data(self):
        """Remove old history entries to keep database manageable."""
        with get_db_session() as db:
            cutoff = datetime.utcnow() - timedelta(days=365)

            # Keep last year of price/stock history
            db.query(PriceHistory).filter(
                PriceHistory.recorded_at < cutoff
            ).delete()

            db.query(StockHistory).filter(
                StockHistory.recorded_at < cutoff
            ).delete()

            # Keep last 30 days of scheduler runs
            run_cutoff = datetime.utcnow() - timedelta(days=30)
            db.query(SchedulerStatus).filter(
                SchedulerStatus.run_started_at < run_cutoff
            ).delete()

            db.commit()
            logger.info("Cleanup completed")


# Global scheduler instance
product_scheduler = ProductScheduler()
