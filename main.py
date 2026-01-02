#!/usr/bin/env python3
"""
Costco UK Stock Tracker - Main Entry Point

Usage:
    python main.py              # Run the web server
    python main.py --setup      # Run interactive setup
    python main.py --encrypt    # Encrypt a password for config
"""

import os
import sys
import argparse
import asyncio
import logging

import uvicorn

from app.config import settings
from app.database import init_db


def setup_logging():
    """Configure logging."""
    log_level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.data_dir / "costco_tracker.log"),
        ]
    )


def run_server():
    """Start the web server."""
    setup_logging()
    init_db()

    print(f"\n{'='*50}")
    print("Costco UK Stock Tracker")
    print(f"{'='*50}")
    print(f"Web UI: http://localhost:8000")
    print(f"API Docs: http://localhost:8000/api/docs")
    print(f"{'='*50}\n")

    uvicorn.run(
        "app.routes:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


def run_setup():
    """Interactive setup wizard."""
    from app.security import PasswordManager, CredentialEncryption
    from app.database import get_db_session
    from app.models import SystemSettings

    print("\n" + "="*50)
    print("Costco UK Stock Tracker - Setup Wizard")
    print("="*50 + "\n")

    # Initialize database
    init_db()

    # Check if already set up
    with get_db_session() as db:
        existing = db.query(SystemSettings).filter(
            SystemSettings.key == "site_password_hash"
        ).first()

        if existing:
            confirm = input("Setup already complete. Reconfigure? [y/N]: ")
            if confirm.lower() != 'y':
                print("Exiting.")
                return

    # Get site password
    while True:
        password = input("Enter site password (min 8 chars): ")
        if len(password) >= 8:
            confirm = input("Confirm password: ")
            if password == confirm:
                break
            print("Passwords don't match. Try again.")
        else:
            print("Password too short. Try again.")

    password_hash = PasswordManager.hash_password(password)

    # Save to database
    with get_db_session() as db:
        setting = db.query(SystemSettings).filter(
            SystemSettings.key == "site_password_hash"
        ).first()

        if setting:
            setting.value = password_hash
        else:
            db.add(SystemSettings(key="site_password_hash", value=password_hash))

    print("\nSite password configured!")

    # Optional: Costco credentials
    print("\n--- Optional: Costco Account (for assisted checkout) ---")
    print("WARNING: Using automated login may violate Costco ToS.")
    setup_costco = input("Configure Costco account? [y/N]: ")

    if setup_costco.lower() == 'y':
        email = input("Costco email: ")
        costco_password = input("Costco password: ")

        encrypted = CredentialEncryption.encrypt(costco_password)

        print("\nAdd these to your .env file:")
        print(f"COSTCO_EMAIL={email}")
        print(f"COSTCO_PASSWORD_ENCRYPTED={encrypted}")
        print("AUTO_ADD_TO_BASKET_ENABLED=true")

    # Notifications
    print("\n--- Notification Setup ---")
    print("Configure notifications by editing .env file.")
    print("See .env.example for all options.")

    print("\n" + "="*50)
    print("Setup complete!")
    print("="*50)
    print("\nNext steps:")
    print("1. Edit .env file with your notification settings")
    print("2. Run: python main.py")
    print("3. Open http://localhost:8000")
    print("")


def encrypt_password():
    """Encrypt a password for the config file."""
    from app.security import CredentialEncryption

    password = input("Enter password to encrypt: ")
    encrypted = CredentialEncryption.encrypt(password)
    print(f"\nEncrypted value:\n{encrypted}")
    print("\nAdd this to your .env file as COSTCO_PASSWORD_ENCRYPTED")


def main():
    parser = argparse.ArgumentParser(description="Costco UK Stock Tracker")
    parser.add_argument("--setup", action="store_true", help="Run setup wizard")
    parser.add_argument("--encrypt", action="store_true", help="Encrypt a password")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")

    args = parser.parse_args()

    if args.debug:
        os.environ["DEBUG"] = "true"

    if args.setup:
        run_setup()
    elif args.encrypt:
        encrypt_password()
    else:
        run_server()


if __name__ == "__main__":
    main()
