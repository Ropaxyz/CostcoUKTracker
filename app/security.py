"""
Security utilities: password hashing, session management, encryption.
"""

import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from cryptography.fernet import Fernet
from base64 import urlsafe_b64encode, urlsafe_b64decode

from app.config import settings


class PasswordManager:
    """Handle password hashing and verification."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password using bcrypt."""
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify a password against its hash."""
        try:
            password_bytes = password.encode('utf-8')
            hashed_bytes = hashed.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hashed_bytes)
        except Exception:
            return False


class SessionManager:
    """Simple session token management."""

    _sessions: dict[str, dict] = {}

    @classmethod
    def create_session(cls, extra_data: Optional[dict] = None) -> str:
        """Create a new session and return the token."""
        token = secrets.token_urlsafe(32)
        cls._sessions[token] = {
            "created_at": datetime.utcnow(),
            "last_activity": datetime.utcnow(),
            "data": extra_data or {}
        }
        return token

    @classmethod
    def validate_session(cls, token: str) -> bool:
        """Check if a session token is valid and not expired."""
        if token not in cls._sessions:
            return False

        session = cls._sessions[token]
        timeout = timedelta(minutes=settings.session_timeout_minutes)

        if datetime.utcnow() - session["last_activity"] > timeout:
            del cls._sessions[token]
            return False

        # Update last activity
        session["last_activity"] = datetime.utcnow()
        return True

    @classmethod
    def destroy_session(cls, token: str) -> None:
        """Remove a session."""
        cls._sessions.pop(token, None)

    @classmethod
    def cleanup_expired(cls) -> int:
        """Remove all expired sessions. Returns count of removed."""
        timeout = timedelta(minutes=settings.session_timeout_minutes)
        now = datetime.utcnow()
        expired = [
            token for token, session in cls._sessions.items()
            if now - session["last_activity"] > timeout
        ]
        for token in expired:
            del cls._sessions[token]
        return len(expired)


class CredentialEncryption:
    """Encrypt/decrypt sensitive credentials (like Costco password)."""

    _key: Optional[bytes] = None

    @classmethod
    def _get_key(cls) -> bytes:
        """Derive encryption key from secret_key."""
        if cls._key is None:
            # Derive a Fernet-compatible key from secret_key
            key_bytes = hashlib.sha256(settings.secret_key.encode()).digest()
            cls._key = urlsafe_b64encode(key_bytes)
        return cls._key

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """Encrypt a string."""
        if not plaintext:
            return ""
        f = Fernet(cls._get_key())
        return f.encrypt(plaintext.encode()).decode()

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        """Decrypt a string."""
        if not ciphertext:
            return ""
        try:
            f = Fernet(cls._get_key())
            return f.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ""


def check_ip_allowed(client_ip: str) -> bool:
    """Check if client IP is in allowlist (if configured)."""
    allowed_ips = settings.allowed_ip_list
    if not allowed_ips:
        return True  # No restrictions
    return client_ip in allowed_ips or client_ip == "127.0.0.1"
