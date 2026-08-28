import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gatekeep.email.backends import ConsoleEmailBackend, SmtpEmailBackend
from gatekeep.email.messages import (
    build_approval_email,
    build_reset_email,
    build_verification_email,
)


@pytest.mark.asyncio
async def test_console_backend_logs_message(caplog):
    with caplog.at_level(logging.INFO):
        await ConsoleEmailBackend().send("u@x.com", "Subj", "Body here")
    assert "u@x.com" in caplog.text and "Body here" in caplog.text


@pytest.mark.asyncio
async def test_smtp_backend_does_not_double_negotiate_tls():
    """aiosmtplib auto-negotiates STARTTLS on connect when use_tls is set, so the
    backend must delegate that to the constructor instead of also calling
    smtp.starttls() itself - doing both raises "Connection already using TLS"."""
    smtp_instance = AsyncMock()
    smtp_instance.__aenter__.return_value = smtp_instance
    smtp_cls = Mock(return_value=smtp_instance)

    with patch("gatekeep.email.backends.aiosmtplib.SMTP", smtp_cls):
        backend = SmtpEmailBackend(
            host="smtp.example.com",
            port=587,
            user="u",
            password="p",
            use_tls=True,
            sender="from@example.com",
        )
        await backend.send("to@example.com", "Subj", "Body")

    smtp_cls.assert_called_once_with(hostname="smtp.example.com", port=587, start_tls=True)
    smtp_instance.starttls.assert_not_called()


def test_message_builders_include_link_and_token():
    subj, body = build_verification_email("https://gk.example", "TOKEN123")
    assert "https://gk.example/verify-email?token=TOKEN123" in body and subj
    subj, body = build_reset_email("https://gk.example", "RTOK")
    assert "https://gk.example/reset-password?token=RTOK" in body and subj
    subj, body = build_approval_email("https://gk.example")
    assert "https://gk.example/login" in body and subj
