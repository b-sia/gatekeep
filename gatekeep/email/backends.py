from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger(__name__)


class EmailBackend(Protocol):
    """Sends a plaintext email. Implementations must not raise on normal sends."""

    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailBackend:
    """Dev/test backend: logs the message (including any link) instead of sending."""

    def send(self, to: str, subject: str, body: str) -> None:
        """Log the email at INFO so dev/test can read verification/reset links."""
        logger.info("[email] to=%s subject=%s\n%s", to, subject, body)


class SmtpEmailBackend:
    """Production backend sending via SMTP with stdlib smtplib (no third-party SDK)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str | None,
        password: str | None,
        use_tls: bool,
        sender: str,
    ) -> None:
        """Store SMTP connection parameters and the From address."""
        self._host, self._port = host, port
        self._user, self._password = user, password
        self._use_tls, self._sender = use_tls, sender

    def send(self, to: str, subject: str, body: str) -> None:
        """Send a plaintext email over SMTP, using STARTTLS/login when configured."""
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self._host, self._port) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._user:
                smtp.login(self._user, self._password or "")
            smtp.send_message(msg)
