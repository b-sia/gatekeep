from __future__ import annotations


def build_verification_email(base_url: str, token: str) -> tuple[str, str]:
    """Return (subject, body) for the email-verification link."""
    link = f"{base_url}/verify-email?token={token}"
    return ("Verify your Gatekeep email", f"Confirm your email to continue:\n{link}\n")


def build_reset_email(base_url: str, token: str) -> tuple[str, str]:
    """Return (subject, body) for the password-reset link."""
    link = f"{base_url}/reset-password?token={token}"
    return ("Reset your Gatekeep password", f"Reset your password here:\n{link}\n")


def build_approval_email(base_url: str) -> tuple[str, str]:
    """Return (subject, body) telling a user their account was approved."""
    link = f"{base_url}/login"
    return ("Your Gatekeep account is approved", f"You can now sign in:\n{link}\n")
