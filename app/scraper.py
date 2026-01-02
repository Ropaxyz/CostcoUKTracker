"""
Costco UK scraper engine.
Handles fetching product pages, parsing stock/price data, and error handling.
"""

import re
import random
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, NamedTuple
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models import StockStatus

logger = logging.getLogger(__name__)


@dataclass
class ProductData:
    """Scraped product data."""
    item_number: str
    name: Optional[str] = None
    price: Optional[float] = None
    stock_status: StockStatus = StockStatus.UNKNOWN
    image_url: Optional[str] = None
    is_warehouse_only: bool = False
    checkout_discount: Optional[float] = None  # Additional discount at checkout
    checkout_discount_text: Optional[str] = None  # Description of the discount
    error: Optional[str] = None
    raw_html: Optional[str] = None

    @property
    def effective_price(self) -> Optional[float]:
        """Calculate the actual price after checkout discount."""
        if self.price is None:
            return None
        if self.checkout_discount:
            return max(0, self.price - self.checkout_discount)
        return self.price


class CostcoScraper:
    """
    Scraper for Costco UK product pages.

    Features:
    - User agent rotation
    - Rate limiting with backoff
    - Graceful error handling
    - Detection of bot blocking
    """

    COSTCO_UK_BASE = "https://www.costco.co.uk"

    # Patterns for detecting stock status from HTML
    STOCK_PATTERNS = {
        "out_of_stock": [
            r'class="[^"]*outOfStock[^"]*"',
            r'>Out of Stock<',
            r'disabled="disabled"[^>]*>Out of Stock',
            r'btn-primary disabled outOfStock',
        ],
        "in_stock": [
            r'id="add-to-cart-button"',
            r'>Add to cart<',
            r'data-cy="addtocart-button',
            r'class="[^"]*add-to-cart__btn[^"]*"[^>]*>Add to cart',
        ],
        "warehouse_only": [
            r'warehouse only',
            r'in-warehouse',
            r'Available in Warehouse',
        ],
    }

    # Patterns for extracting data
    PRICE_PATTERNS = [
        r'<span[^>]*class="[^"]*notranslate[^"]*"[^>]*>£([\d,]+\.?\d*)</span>',
        r'"price":\s*"?([\d.]+)"?',
        r'£([\d,]+\.?\d*)',
        r'data-product-price="([\d.]+)"',
    ]

    ITEM_NUMBER_PATTERNS = [
        r'productCodePost[^>]*value="(\d+)"',
        r'data-cy="addtocart-button-(\d+)"',
        r'Item\s*#?\s*:?\s*(\d{5,7})',
        r'/p/(\d+)',
    ]

    NAME_PATTERNS = [
        r'<h1[^>]*class="[^"]*product-name[^"]*"[^>]*>([^<]+)</h1>',
        r'<title>([^|<]+)',
        r'"name":\s*"([^"]+)"',
    ]

    IMAGE_PATTERNS = [
        r'<img[^>]*class="[^"]*product-image[^"]*"[^>]*src="([^"]+)"',
        r'"image":\s*"([^"]+)"',
        r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"',
    ]

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request_time: Optional[datetime] = None
        self._consecutive_errors = 0
        self._backoff_until: Optional[datetime] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.request_timeout_seconds),
                follow_redirects=True,
                headers=self._get_headers(),
            )
        return self._client

    def _get_headers(self) -> dict:
        """Get randomized headers."""
        user_agent = random.choice(settings.user_agent_list)
        return {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Cache-Control": "max-age=0",
        }

    def _get_delay(self) -> float:
        """Calculate delay between requests (with jitter)."""
        if settings.safe_mode:
            base_delay = 3.0 + (self._consecutive_errors * 2)
        else:
            base_delay = 1.0 + (self._consecutive_errors * 1)

        # Add random jitter
        jitter = random.uniform(0.5, 2.0)
        return base_delay * jitter

    async def _wait_if_needed(self):
        """Respect rate limiting."""
        # Check backoff
        if self._backoff_until and datetime.utcnow() < self._backoff_until:
            wait_seconds = (self._backoff_until - datetime.utcnow()).total_seconds()
            logger.info(f"Backing off for {wait_seconds:.1f}s")
            await asyncio.sleep(wait_seconds)
            self._backoff_until = None

        # Normal delay
        if self._last_request_time:
            elapsed = (datetime.utcnow() - self._last_request_time).total_seconds()
            delay = self._get_delay()
            if elapsed < delay:
                await asyncio.sleep(delay - elapsed)

    def _build_product_url(self, item_number: str) -> str:
        """Build product URL from item number."""
        return f"{self.COSTCO_UK_BASE}/p/{item_number}"

    def _detect_blocking(self, html: str, status_code: int) -> Optional[str]:
        """Detect if we're being blocked."""
        if status_code == 403:
            return "Access forbidden (403)"
        if status_code == 429:
            return "Rate limited (429)"
        if status_code >= 500:
            return f"Server error ({status_code})"

        # Check for CAPTCHA or blocking pages
        blocking_indicators = [
            "captcha",
            "robot",
            "blocked",
            "access denied",
            "please verify",
            "security check",
        ]
        html_lower = html.lower()
        for indicator in blocking_indicators:
            if indicator in html_lower and len(html) < 10000:
                return f"Possible blocking detected: {indicator}"

        return None

    def _parse_price(self, html: str) -> Optional[float]:
        """Extract price from HTML."""
        for pattern in self.PRICE_PATTERNS:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                try:
                    price_str = match.group(1).replace(",", "")
                    price = float(price_str)
                    if 0 < price < 100000:  # Sanity check
                        return price
                except (ValueError, IndexError):
                    continue
        return None

    def _parse_stock_status(self, html: str) -> StockStatus:
        """Determine stock status from HTML."""
        # Check for out of stock first (more specific)
        for pattern in self.STOCK_PATTERNS["out_of_stock"]:
            if re.search(pattern, html, re.IGNORECASE):
                return StockStatus.OUT_OF_STOCK

        # Check for warehouse only
        for pattern in self.STOCK_PATTERNS["warehouse_only"]:
            if re.search(pattern, html, re.IGNORECASE):
                return StockStatus.WAREHOUSE_ONLY

        # Check for in stock
        for pattern in self.STOCK_PATTERNS["in_stock"]:
            if re.search(pattern, html, re.IGNORECASE):
                return StockStatus.IN_STOCK

        return StockStatus.UNKNOWN

    def _parse_item_number(self, html: str, url: str) -> Optional[str]:
        """Extract item number from HTML or URL."""
        # Try URL first
        url_match = re.search(r'/p/(\d+)', url)
        if url_match:
            return url_match.group(1)

        # Try HTML patterns
        for pattern in self.ITEM_NUMBER_PATTERNS:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)

        return None

    def _parse_name(self, html: str) -> Optional[str]:
        """Extract product name from HTML."""
        for pattern in self.NAME_PATTERNS:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Clean up
                name = re.sub(r'\s+', ' ', name)
                name = name.replace(" | Costco UK", "").strip()
                if len(name) > 5:
                    return name[:500]
        return None

    def _parse_image(self, html: str) -> Optional[str]:
        """Extract product image URL."""
        for pattern in self.IMAGE_PATTERNS:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                img_url = match.group(1)
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                elif img_url.startswith("/"):
                    img_url = self.COSTCO_UK_BASE + img_url
                return img_url
        return None

    def _parse_checkout_discount(self, html: str) -> tuple[Optional[float], Optional[str]]:
        """
        Extract checkout discount information.
        Returns (discount_amount, discount_text)
        """
        # Pattern 1: "A further £300 reduction automatically applied at checkout"
        match = re.search(
            r'(?:further|additional)\s*£([\d,]+\.?\d*)\s*(?:reduction|discount|off).*?(?:checkout|basket)',
            html,
            re.IGNORECASE
        )
        if match:
            amount_str = match.group(1).replace(',', '')
            try:
                amount = float(amount_str)
                # Extract the full discount text
                text_match = re.search(
                    r'<[^>]*>(.*?(?:further|additional).*?(?:checkout|basket)[^<]*)</[^>]*>',
                    html,
                    re.IGNORECASE | re.DOTALL
                )
                discount_text = text_match.group(1).strip() if text_match else f"£{amount} reduction at checkout"
                # Clean up HTML tags
                discount_text = re.sub(r'<[^>]+>', '', discount_text)
                return amount, discount_text
            except ValueError:
                pass

        # Pattern 2: Look for promotion section
        promo_match = re.search(
            r'<sip-product-promotion-section[^>]*>.*?<b>(.*?)</b>.*?</sip-product-promotion-section>',
            html,
            re.IGNORECASE | re.DOTALL
        )
        if promo_match:
            promo_text = promo_match.group(1).strip()
            # Extract amount from promo text
            amount_match = re.search(r'£([\d,]+\.?\d*)', promo_text)
            if amount_match:
                amount_str = amount_match.group(1).replace(',', '')
                try:
                    return float(amount_str), promo_text
                except ValueError:
                    pass
            # If no amount found, return the text anyway
            return None, promo_text

        return None, None

    async def fetch_product(self, url_or_item: str) -> ProductData:
        """
        Fetch and parse a product page.

        Args:
            url_or_item: Either a full URL or an item number

        Returns:
            ProductData with scraped information
        """
        # Check kill switch
        if settings.kill_switch:
            return ProductData(
                item_number=url_or_item,
                error="Kill switch is active - automation disabled"
            )

        # Determine URL
        if url_or_item.startswith("http"):
            url = url_or_item
            item_number = self._parse_item_number("", url) or url_or_item
        else:
            item_number = url_or_item
            url = self._build_product_url(item_number)

        try:
            await self._wait_if_needed()

            client = await self._get_client()

            # Rotate user agent per request
            headers = self._get_headers()

            logger.debug(f"Fetching {url}")
            self._last_request_time = datetime.utcnow()

            response = await client.get(url, headers=headers)
            html = response.text

            # Check for blocking
            blocking_error = self._detect_blocking(html, response.status_code)
            if blocking_error:
                self._consecutive_errors += 1
                self._trigger_backoff()
                return ProductData(
                    item_number=item_number,
                    error=blocking_error
                )

            # Check for 404 / removed product
            if response.status_code == 404:
                return ProductData(
                    item_number=item_number,
                    stock_status=StockStatus.REMOVED,
                    error="Product not found (404)"
                )

            if response.status_code != 200:
                self._consecutive_errors += 1
                return ProductData(
                    item_number=item_number,
                    error=f"HTTP {response.status_code}"
                )

            # Parse the page
            self._consecutive_errors = 0  # Reset on success

            parsed_item = self._parse_item_number(html, url) or item_number
            checkout_discount, checkout_discount_text = self._parse_checkout_discount(html)

            return ProductData(
                item_number=parsed_item,
                name=self._parse_name(html),
                price=self._parse_price(html),
                stock_status=self._parse_stock_status(html),
                image_url=self._parse_image(html),
                is_warehouse_only=self._parse_stock_status(html) == StockStatus.WAREHOUSE_ONLY,
                checkout_discount=checkout_discount,
                checkout_discount_text=checkout_discount_text,
            )

        except httpx.TimeoutException:
            self._consecutive_errors += 1
            return ProductData(
                item_number=item_number,
                error="Request timeout"
            )
        except httpx.RequestError as e:
            self._consecutive_errors += 1
            return ProductData(
                item_number=item_number,
                error=f"Request error: {str(e)}"
            )
        except Exception as e:
            self._consecutive_errors += 1
            logger.exception(f"Unexpected error fetching {url}")
            return ProductData(
                item_number=item_number,
                error=f"Unexpected error: {str(e)}"
            )

    def _trigger_backoff(self):
        """Set backoff time based on consecutive errors."""
        backoff_seconds = min(
            60 * (settings.backoff_multiplier ** self._consecutive_errors),
            3600  # Max 1 hour
        )
        self._backoff_until = datetime.utcnow() + timedelta(seconds=backoff_seconds)
        logger.warning(f"Triggering backoff for {backoff_seconds:.0f}s")

    async def close(self):
        """Close HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None


# Global scraper instance
scraper = CostcoScraper()
