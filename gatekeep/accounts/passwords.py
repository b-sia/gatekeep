from __future__ import annotations

from passlib.context import CryptContext

MIN_PASSWORD_LENGTH = 8

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    """Return a salted bcrypt hash of a plaintext password."""
    return _pwd_context.hash(raw)


def verify_password(raw: str, password_hash: str) -> bool:
    """Return True if `raw` matches the stored bcrypt `password_hash`."""
    return _pwd_context.verify(raw, password_hash)


def validate_password_strength(raw: str) -> str:
    """Return `raw` unchanged if it meets the minimum length requirement.

    Raises:
        ValueError: if `raw` is shorter than `MIN_PASSWORD_LENGTH`.
    """
    if len(raw) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    return raw
