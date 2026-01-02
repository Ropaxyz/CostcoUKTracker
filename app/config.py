"""
Configuration management for Costco Tracker.
Loads settings from environment variables and .env file.
"""

import os
import secrets
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings with validation."""

    # App settings
    app_name: str = "Costco UK Stock Tracker"
    debug: bool = False
    secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))

    # Database
    database_url: str = "sqlite:///./data/costco_tracker.db"

    # Security
    site_password_hash: Optional[str] = None
    session_timeout_minutes: int = 1440  # 24 hours
    allowed_ips: str = ""  # Comma-separated, empty = all allowed
    secure_cookies: bool = False  # Set to True only if using HTTPS

    # Costco settings
    costco_base_url: str = "https://www.costco.co.uk"
    default_poll_interval_minutes: int = 45
    min_poll_interval_minutes: int = 15
    max_poll_interval_minutes: int = 180
    request_timeout_seconds: int = 30
    max_retries: int = 3
    backoff_multiplier: float = 2.0

    # User agent rotation
    user_agents: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36|"
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36|"
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
    )

    # Notifications - Email
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True

    # Notifications - Telegram
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Notifications - Discord
    discord_enabled: bool = False
    discord_webhook_url: str = ""

    # Notifications - Pushover
    pushover_enabled: bool = False
    pushover_app_token: str = ""
    pushover_user_key: str = ""

    # Assisted checkout (optional)
    costco_email: str = ""
    costco_password_encrypted: str = ""
    auto_add_to_basket_enabled: bool = False

    # Modes
    safe_mode: bool = True  # Limits request frequency
    kill_switch: bool = False  # Stops all automation

    # Data paths
    data_dir: Path = Path("./data")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def user_agent_list(self) -> list[str]:
        return [ua.strip() for ua in self.user_agents.split("|") if ua.strip()]

    @property
    def allowed_ip_list(self) -> list[str]:
        if not self.allowed_ips:
            return []
        return [ip.strip() for ip in self.allowed_ips.split(",") if ip.strip()]


settings = Settings()

# Ensure data directory exists
settings.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings_from_db():
    """Load settings from database and update the settings object."""
    from app.database import SessionLocal
    from app.models import SystemSettings

    db = SessionLocal()
    try:
        db_settings = db.query(SystemSettings).all()
        for setting in db_settings:
            key = setting.key
            value = setting.value

            # Skip password hash (handled separately)
            if key == "site_password_hash":
                continue

            # Convert value to appropriate type
            if hasattr(settings, key):
                current_value = getattr(settings, key)

                # Convert string to appropriate type
                if isinstance(current_value, bool):
                    value = value.lower() in ("true", "1", "yes")
                elif isinstance(current_value, int):
                    value = int(value) if value else 0
                elif isinstance(current_value, float):
                    value = float(value) if value else 0.0

                setattr(settings, key, value)
    finally:
        db.close()
