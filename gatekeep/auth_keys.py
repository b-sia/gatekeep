from __future__ import annotations

import hashlib
import secrets


def generate_key() -> str:
    return "gk-" + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
