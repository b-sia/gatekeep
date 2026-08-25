from __future__ import annotations

import hashlib
import secrets


def generate_key() -> str:
    """Generate a new random, URL-safe raw API key with a 'gk-' prefix."""
    return "gk-" + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    """Return the sha256 hex digest of a raw API key, for storage/lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
