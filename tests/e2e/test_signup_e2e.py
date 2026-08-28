"""End-to-end coverage of the full self-serve signup flow.

Exercises signup -> email verification -> pending login -> operator approval
-> approved login -> key minting -> a real gateway completion request, all
through the ASGI app with no component mocked except the LLM provider.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

import gatekeep.app as app_module
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import ApiKey
from tests.helpers import FakeProvider, create_account


@pytest_asyncio.fixture
async def client(monkeypatch):
    """An httpx client driving the real app over https, with fake LLM providers.

    The signup-to-key flow authenticates later requests via a Secure
    `gk_session` cookie; httpx will not resend a Secure cookie over plain
    http, so the base_url must be https (see tests/api/test_dashboard_session_auth.py).
    """
    fake = FakeProvider(["pong"])
    monkeypatch.setitem(app_module._providers, "anthropic", fake)
    monkeypatch.setitem(app_module._providers, "ollama", fake)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c


async def _operator_key() -> str:
    """Create an operator account with a live API key and return the raw key."""
    async with SessionLocal() as s:
        raw = generate_key()
        op = await create_account(s, is_operator=True)
        s.add(ApiKey(name="op", key_hash=hash_key(raw), account_id=op.id))
        await s.commit()
    return raw


@pytest.mark.asyncio
async def test_full_signup_to_gateway(client, caplog):
    """Drive signup through to a real gateway request using only the raw minted key."""
    auth = "/dashboard/api/auth"

    with caplog.at_level(logging.INFO):
        r = await client.post(f"{auth}/signup", json={"email": "e2e@x.com", "password": "pw123456"})
    assert r.status_code == 202
    token = caplog.text.split("token=")[1].split()[0].strip()
    assert (await client.post(f"{auth}/verify-email", json={"token": token})).status_code == 200

    # Pending login: succeeds (only rejected/disabled accounts are blocked at
    # login), but the account is not yet approved.
    r = await client.post(f"{auth}/login", json={"email": "e2e@x.com", "password": "pw123456"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    acct_id = r.json()["account_id"]

    # Operator approves with a monthly budget. This must go through a
    # separate, cookie-free client: `_require_caller_account` tries the
    # session cookie before the API key, and `client`'s cookie jar already
    # carries the signup user's session from the login above - reusing
    # `client` here would resolve the caller to that (non-operator) account
    # and get a 403 instead of exercising the operator's Bearer auth.
    op_raw_key = await _operator_key()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as op_client:
        r = await op_client.post(
            f"/dashboard/api/accounts/{acct_id}/approve",
            json={"monthly_budget_usd": 50.0},
            headers={"Authorization": f"Bearer {op_raw_key}"},
        )
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # User logs in again now that they're approved, and mints a key via
    # cookie + CSRF auth.
    login = await client.post(f"{auth}/login", json={"email": "e2e@x.com", "password": "pw123456"})
    assert login.status_code == 200
    assert login.json()["status"] == "approved"
    csrf = login.json()["csrf_token"]
    r = await client.post(
        f"/dashboard/api/accounts/{acct_id}/keys",
        json={"name": "primary"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200
    raw_key = r.json()["key"]

    # The freshly minted raw key authenticates a real gateway completion
    # request through the fake provider.
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "pong"
