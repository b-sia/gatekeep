import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.app import app
from gatekeep.config import get_settings

BASE = "/dashboard/api/auth"


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_signup_then_login_flow_sets_session_cookie(client, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        r = await client.post(f"{BASE}/signup", json={"email": "e@x.com", "password": "pw123456"})
    assert r.status_code == 202
    # console email backend logged the verification link; extract the token
    token = caplog.text.split("token=")[1].split()[0].strip()
    r = await client.post(f"{BASE}/verify-email", json={"token": token})
    assert r.status_code == 200
    r = await client.post(f"{BASE}/login", json={"email": "e@x.com", "password": "pw123456"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert "gk_session" in r.cookies


@pytest.mark.asyncio
async def test_signup_duplicate_returns_409_with_message(client):
    await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    r = await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    assert r.status_code == 409
    assert "already exists" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_login_bad_password_returns_401(client):
    r = await client.post(f"{BASE}/login", json={"email": "no@x.com", "password": "bad"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_is_pre_auth_rate_limited_per_ip(client, monkeypatch):
    """Repeated calls to /login from one client eventually hit the per-IP
    pre-auth token bucket and get a 429 - proving `_enforce_pre_auth_rate_limit`
    actually runs on the auth routes (it doesn't run via `require_api_key`,
    since these routes don't use that dependency)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "pre_auth_rate_limit_tokens_per_min", 2)
    monkeypatch.setattr(settings, "pre_auth_rate_limit_refill_rate", 2 / 60)

    statuses = []
    for _ in range(5):
        r = await client.post(f"{BASE}/login", json={"email": "no@x.com", "password": "bad"})
        statuses.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in statuses
    # Earlier, within-capacity calls still behave exactly like before (401,
    # not some altered success/error shape).
    assert statuses[0] == 401
