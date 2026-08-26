from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    """Generate a random URL-safe opaque token (for sessions and email links)."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest of a raw token, for storage and lookup."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
