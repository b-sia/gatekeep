from __future__ import annotations

from gatekeep.config import get_settings
from gatekeep.email.backends import ConsoleEmailBackend, EmailBackend, SmtpEmailBackend


def get_email_backend() -> EmailBackend:
    """Return the configured email backend (console for dev/test, smtp for prod)."""
    s = get_settings()
    if s.email_backend == "smtp":
        return SmtpEmailBackend(
            host=s.smtp_host,
            port=s.smtp_port,
            user=s.smtp_user,
            password=s.smtp_password,
            use_tls=s.smtp_use_tls,
            sender=s.email_from,
        )
    return ConsoleEmailBackend()
