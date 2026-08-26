from __future__ import annotations

from passlib.context import CryptContext

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    """Return a salted bcrypt hash of a plaintext password."""
    return _pwd_context.hash(raw)


def verify_password(raw: str, password_hash: str) -> bool:
    """Return True if `raw` matches the stored bcrypt `password_hash`."""
    return _pwd_context.verify(raw, password_hash)
