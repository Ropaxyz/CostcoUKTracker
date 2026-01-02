"""
Notification system supporting multiple channels.
"""

import logging
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from datetime import datetime
from dataclasses import dataclass

import httpx

from app.config import settings
from app.models import Product, AlertType, NotificationChannel

logger = logging.getLogger(__name__)


@dataclass
class NotificationResult:
    """Result of sending a notification."""
    channel: str
    success: bool
    error: Optional[str] = None


class NotificationService:
    """
    Multi-channel notification service.
    Supports: Email, Telegram, Discord, Pushover
    """

    def __init__(self):
        self._http_client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30)
        return self._http_client

    def _format_message(
        self,
        product: Product,
        alert_type: AlertType,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
    ) -> tuple[str, str]:
        """Format notification message. Returns (subject, body)."""

        product_url = f"{settings.costco_base_url}/p/{product.item_number}"
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        if alert_type == AlertType.BACK_IN_STOCK:
            subject = f"Back in Stock: {product.name or product.item_number}"
            body = f"""
{product.name or 'Product'}

Item #{product.item_number} is back in stock!

Current Price: £{product.current_price:.2f if product.current_price else 'N/A'}
Status: {new_value or 'In Stock'}

{product_url}

Checked at: {timestamp}
"""

        elif alert_type == AlertType.PRICE_DROP:
            subject = f"Price Drop: {product.name or product.item_number}"
            change = ""
            if old_value and new_value:
                try:
                    old_p = float(old_value)
                    new_p = float(new_value)
                    pct = ((old_p - new_p) / old_p) * 100
                    change = f" ({pct:.1f}% off)"
                except ValueError:
                    pass

            body = f"""
{product.name or 'Product'}

Price dropped!{change}

Old Price: £{old_value}
New Price: £{new_value}
{'LOWEST EVER!' if product.current_price and product.lowest_price and product.current_price <= product.lowest_price else ''}
{f'Target: £{product.target_price:.2f}' if product.target_price else ''}

{product_url}

Checked at: {timestamp}
"""

        elif alert_type == AlertType.TARGET_PRICE:
            subject = f"Target Price Reached: {product.name or product.item_number}"
            body = f"""
{product.name or 'Product'}

Target price reached!

Current Price: £{product.current_price:.2f if product.current_price else 'N/A'}
Your Target: £{product.target_price:.2f if product.target_price else 'N/A'}

{product_url}

Checked at: {timestamp}
"""

        elif alert_type == AlertType.LOWEST_EVER:
            subject = f"Lowest Ever Price: {product.name or product.item_number}"
            body = f"""
{product.name or 'Product'}

LOWEST PRICE EVER recorded!

Current Price: £{product.current_price:.2f if product.current_price else 'N/A'}
Previous Lowest: £{old_value}

{product_url}

Checked at: {timestamp}
"""

        elif alert_type == AlertType.ADDED_TO_BASKET:
            subject = f"Added to Basket: {product.name or product.item_number}"
            body = f"""
{product.name or 'Product'}

Item automatically added to your Costco basket!

Price: £{product.current_price:.2f if product.current_price else 'N/A'}
Quantity: {product.auto_add_quantity}

WARNING: Complete your purchase soon - items may sell out!

Checkout: https://www.costco.co.uk/cart

{product_url}

Added at: {timestamp}
"""

        else:
            subject = f"Costco Alert: {product.name or product.item_number}"
            body = f"""
{product.name or 'Product'}

Alert: {alert_type.value}

Old: {old_value}
New: {new_value}

{product_url}

{timestamp}
"""

        return subject.strip(), body.strip()

    async def send_email(
        self,
        subject: str,
        body: str,
        to_email: Optional[str] = None,
    ) -> NotificationResult:
        """Send email notification."""
        if not settings.smtp_enabled:
            return NotificationResult("email", False, "Email not configured")

        try:
            msg = MIMEMultipart()
            msg["From"] = settings.smtp_from_email
            msg["To"] = to_email or settings.smtp_from_email
            msg["Subject"] = subject

            msg.attach(MIMEText(body, "plain"))

            # Run in executor to not block
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._send_email_sync, msg)

            logger.info(f"Email sent: {subject}")
            return NotificationResult("email", True)

        except Exception as e:
            logger.error(f"Email failed: {e}")
            return NotificationResult("email", False, str(e))

    def _send_email_sync(self, msg: MIMEMultipart):
        """Synchronous email sending."""
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_use_tls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)

    async def send_telegram(self, message: str) -> NotificationResult:
        """Send Telegram notification."""
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            return NotificationResult("telegram", False, "Telegram not configured")

        try:
            client = await self._get_client()
            url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

            response = await client.post(url, json={
                "chat_id": settings.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            })

            if response.status_code == 200:
                logger.info("Telegram message sent")
                return NotificationResult("telegram", True)
            else:
                error = response.json().get("description", response.text)
                return NotificationResult("telegram", False, error)

        except Exception as e:
            logger.error(f"Telegram failed: {e}")
            return NotificationResult("telegram", False, str(e))

    async def send_discord(self, subject: str, body: str) -> NotificationResult:
        """Send Discord webhook notification."""
        if not settings.discord_enabled or not settings.discord_webhook_url:
            return NotificationResult("discord", False, "Discord not configured")

        try:
            client = await self._get_client()

            # Format as Discord embed
            payload = {
                "embeds": [{
                    "title": subject,
                    "description": body[:4000],  # Discord limit
                    "color": 0x005DAB,  # Costco blue
                    "timestamp": datetime.utcnow().isoformat(),
                }]
            }

            response = await client.post(
                settings.discord_webhook_url,
                json=payload,
            )

            if response.status_code in (200, 204):
                logger.info("Discord message sent")
                return NotificationResult("discord", True)
            else:
                return NotificationResult("discord", False, response.text)

        except Exception as e:
            logger.error(f"Discord failed: {e}")
            return NotificationResult("discord", False, str(e))

    async def send_pushover(self, subject: str, body: str) -> NotificationResult:
        """Send Pushover notification."""
        if not settings.pushover_enabled or not settings.pushover_app_token:
            return NotificationResult("pushover", False, "Pushover not configured")

        try:
            client = await self._get_client()

            response = await client.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": settings.pushover_app_token,
                    "user": settings.pushover_user_key,
                    "title": subject,
                    "message": body[:1000],  # Pushover limit
                    "priority": 1,
                },
            )

            if response.status_code == 200:
                logger.info("Pushover message sent")
                return NotificationResult("pushover", True)
            else:
                return NotificationResult("pushover", False, response.text)

        except Exception as e:
            logger.error(f"Pushover failed: {e}")
            return NotificationResult("pushover", False, str(e))

    async def send_notification(
        self,
        product: Product,
        alert_type: AlertType,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        channels: Optional[list[str]] = None,
    ) -> list[NotificationResult]:
        """
        Send notification to all enabled channels for a product.

        Args:
            product: The product triggering the alert
            alert_type: Type of alert
            old_value: Previous value (for comparisons)
            new_value: New value
            channels: Override channels (uses product settings if None)

        Returns:
            List of results for each channel
        """
        if channels is None:
            channels = product.enabled_channels

        subject, body = self._format_message(product, alert_type, old_value, new_value)
        results = []

        tasks = []
        for channel in channels:
            if channel == "email" and settings.smtp_enabled:
                tasks.append(self.send_email(subject, body))
            elif channel == "telegram" and settings.telegram_enabled:
                # Telegram uses HTML formatting
                telegram_msg = f"<b>{subject}</b>\n\n{body}"
                tasks.append(self.send_telegram(telegram_msg))
            elif channel == "discord" and settings.discord_enabled:
                tasks.append(self.send_discord(subject, body))
            elif channel == "pushover" and settings.pushover_enabled:
                tasks.append(self.send_pushover(subject, body))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            # Convert exceptions to results
            results = [
                r if isinstance(r, NotificationResult)
                else NotificationResult("unknown", False, str(r))
                for r in results
            ]

        return results

    async def close(self):
        """Close HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# Global notification service
notifications = NotificationService()
