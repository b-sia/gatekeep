import logging

from gatekeep.email.backends import ConsoleEmailBackend
from gatekeep.email.messages import (
    build_approval_email,
    build_reset_email,
    build_verification_email,
)


def test_console_backend_logs_message(caplog):
    with caplog.at_level(logging.INFO):
        ConsoleEmailBackend().send("u@x.com", "Subj", "Body here")
    assert "u@x.com" in caplog.text and "Body here" in caplog.text


def test_message_builders_include_link_and_token():
    subj, body = build_verification_email("https://gk.example", "TOKEN123")
    assert "https://gk.example/verify-email?token=TOKEN123" in body and subj
    subj, body = build_reset_email("https://gk.example", "RTOK")
    assert "https://gk.example/reset-password?token=RTOK" in body and subj
    subj, body = build_approval_email("https://gk.example")
    assert "https://gk.example/login" in body and subj
