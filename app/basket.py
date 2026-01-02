"""
Assisted checkout functionality.
IMPORTANT: Does NOT store payment details or auto-complete purchases.
Only adds items to basket when conditions are met.
"""

import logging
from datetime import datetime
from typing import Optional, NamedTuple
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.security import CredentialEncryption
from app.database import get_db_session
from app.models import BasketAction

logger = logging.getLogger(__name__)


@dataclass
class BasketResult:
    """Result of basket operation."""
    success: bool
    message: str
    checkout_url: Optional[str] = None
    price: Optional[float] = None


class CostcoSession:
    """
    Manages authenticated Costco UK session.

    ⚠️ IMPORTANT WARNINGS:
    - Credentials are encrypted at rest
    - Credentials are NEVER logged
    - Using automated login may violate Costco ToS
    - Use at your own risk
    """

    COSTCO_UK_BASE = "https://www.costco.co.uk"
    LOGIN_URL = f"{COSTCO_UK_BASE}/LogonForm"
    CART_URL = f"{COSTCO_UK_BASE}/cart"
    ADD_TO_CART_URL = f"{COSTCO_UK_BASE}/rest/v2/uk/users/current/carts"

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._is_authenticated = False
        self._session_cookies: dict = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client with session cookies."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                cookies=self._session_cookies,
            )
        return self._client

    @property
    def is_authenticated(self) -> bool:
        return self._is_authenticated

    async def login(self) -> bool:
        """
        Authenticate with Costco UK.
        Returns True if successful.

        ⚠️ WARNING: May violate Costco Terms of Service
        """
        if not settings.costco_email or not settings.costco_password_encrypted:
            logger.warning("Costco credentials not configured")
            return False

        try:
            # Decrypt password
            password = CredentialEncryption.decrypt(settings.costco_password_encrypted)
            if not password:
                logger.error("Failed to decrypt Costco password")
                return False

            client = await self._get_client()

            # Get login page for CSRF token
            login_page = await client.get(self.LOGIN_URL)
            if login_page.status_code != 200:
                logger.error(f"Failed to load login page: {login_page.status_code}")
                return False

            # Parse for CSRF token
            soup = BeautifulSoup(login_page.text, "html.parser")
            csrf_input = soup.find("input", {"name": "CSRFToken"})
            csrf_token = csrf_input.get("value") if csrf_input else ""

            # Perform login
            login_data = {
                "logonId": settings.costco_email,
                "logonPassword": password,
                "CSRFToken": csrf_token,
            }

            response = await client.post(
                self.LOGIN_URL,
                data=login_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": self.LOGIN_URL,
                }
            )

            # Check if login succeeded
            if "sign out" in response.text.lower() or "my account" in response.text.lower():
                self._is_authenticated = True
                self._session_cookies = dict(client.cookies)
                logger.info("Costco login successful")
                return True
            else:
                logger.warning("Costco login failed - check credentials")
                return False

        except Exception as e:
            logger.exception("Login error")
            return False

    async def logout(self):
        """Clear session."""
        self._is_authenticated = False
        self._session_cookies = {}
        if self._client:
            await self._client.aclose()
            self._client = None

    async def add_to_cart(
        self,
        item_number: str,
        quantity: int = 1
    ) -> BasketResult:
        """
        Add an item to the Costco basket.

        Args:
            item_number: Costco product code
            quantity: Number to add

        Returns:
            BasketResult with success status

        ⚠️ WARNING: Requires authentication. May violate ToS.
        """
        if not self._is_authenticated:
            # Try to login first
            if not await self.login():
                return BasketResult(
                    success=False,
                    message="Not authenticated - login required"
                )

        try:
            client = await self._get_client()

            # Costco uses a specific API endpoint
            add_url = f"{self.ADD_TO_CART_URL}/current/entries"

            payload = {
                "product": {"code": item_number},
                "quantity": quantity,
            }

            response = await client.post(
                add_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )

            if response.status_code in (200, 201):
                logger.info(f"Added {item_number} x{quantity} to basket")

                # Log the action
                await self._log_action(item_number, "success", quantity)

                return BasketResult(
                    success=True,
                    message=f"Added {quantity}x item to basket",
                    checkout_url=self.CART_URL,
                )

            else:
                error_msg = f"Failed to add to cart: {response.status_code}"
                try:
                    error_data = response.json()
                    if "errors" in error_data:
                        error_msg = error_data["errors"][0].get("message", error_msg)
                except Exception:
                    pass

                await self._log_action(item_number, "failed", quantity, error_msg)

                return BasketResult(
                    success=False,
                    message=error_msg
                )

        except Exception as e:
            error_msg = f"Error adding to cart: {str(e)}"
            logger.exception(error_msg)
            await self._log_action(item_number, "failed", quantity, error_msg)

            return BasketResult(
                success=False,
                message=error_msg
            )

    async def verify_cart(self, item_number: str) -> bool:
        """Verify an item is in the cart."""
        if not self._is_authenticated:
            return False

        try:
            client = await self._get_client()
            response = await client.get(self.CART_URL)

            return item_number in response.text

        except Exception:
            return False

    async def _log_action(
        self,
        item_number: str,
        action: str,
        quantity: int,
        message: Optional[str] = None
    ):
        """Log basket action to database."""
        try:
            with get_db_session() as db:
                from app.models import Product

                product = db.query(Product).filter(
                    Product.item_number == item_number
                ).first()

                if product:
                    log = BasketAction(
                        product_id=product.id,
                        action=action,
                        price_at_action=product.current_price,
                        quantity=quantity,
                        message=message,
                    )
                    db.add(log)
                    db.commit()

        except Exception as e:
            logger.error(f"Failed to log basket action: {e}")

    async def close(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Global session instance (singleton)
_costco_session: Optional[CostcoSession] = None


def get_costco_session() -> CostcoSession:
    """Get or create Costco session."""
    global _costco_session
    if _costco_session is None:
        _costco_session = CostcoSession()
    return _costco_session


async def add_to_basket(
    item_number: str,
    quantity: int = 1
) -> BasketResult:
    """
    Convenience function to add item to basket.

    This is the main entry point for the auto-add-to-basket feature.
    """
    if not settings.auto_add_to_basket_enabled:
        return BasketResult(
            success=False,
            message="Auto-add-to-basket is disabled"
        )

    session = get_costco_session()
    return await session.add_to_cart(item_number, quantity)


async def validate_checkout_ready(item_number: str) -> dict:
    """
    Dry-run checkout validation.
    Confirms stock and price before alerting user.

    Returns dict with:
    - stock_available: bool
    - price_confirmed: bool
    - price: float or None
    - delivery_possible: bool
    - message: str
    """
    from app.scraper import scraper
    from app.models import StockStatus

    try:
        data = await scraper.fetch_product(item_number)

        return {
            "stock_available": data.stock_status == StockStatus.IN_STOCK,
            "price_confirmed": data.price is not None,
            "price": data.price,
            "delivery_possible": not data.is_warehouse_only,
            "message": "Validation passed" if (
                data.stock_status == StockStatus.IN_STOCK and
                data.price is not None and
                not data.is_warehouse_only
            ) else "Validation failed"
        }

    except Exception as e:
        return {
            "stock_available": False,
            "price_confirmed": False,
            "price": None,
            "delivery_possible": False,
            "message": f"Validation error: {str(e)}"
        }
