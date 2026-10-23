"""Token encryption, password hashing, signed URLs and secret scrubbing."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().token_encryption_key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:  # pragma: no cover - key rotation problem
        raise RuntimeError("Stored token cannot be decrypted (was TOKEN_ENCRYPTION_KEY changed?)") from exc


# ------------------------------------------------------------------ passwords
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# --------------------------------------------------------- signed media URLs
def sign_media_token(derivative_id: int, ttl_seconds: int = 86400) -> str:
    """Short-lived token that lets Instagram fetch one derivative without logging in."""
    expires = int(time.time()) + ttl_seconds
    msg = f"{derivative_id}.{expires}"
    sig = hmac.new(get_settings().secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{msg}.{sig}"


def verify_media_token(token: str) -> int | None:
    try:
        derivative_id, expires, sig = token.split(".")
    except ValueError:
        return None
    msg = f"{derivative_id}.{expires}"
    expected = hmac.new(get_settings().secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected) or int(expires) < time.time():
        return None
    return int(derivative_id)


# ------------------------------------------------------------- scrubbing
_SECRET_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+", re.I),
    re.compile(r"(client_secret=)[^&\s\"']+", re.I),
    re.compile(r"(\"access_token\"\s*:\s*\")[^\"]+", re.I),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.I),
    re.compile(r"(sk-ant-)[A-Za-z0-9_\-]+"),
]


def scrub(text: str | None) -> str:
    """Remove tokens and secrets from any string before it is logged, stored or displayed."""
    if not text:
        return text or ""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    return text
