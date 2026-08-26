import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.app import app

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
async def test_signup_duplicate_still_returns_202(client):
    await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    r = await client.post(f"{BASE}/signup", json={"email": "d@x.com", "password": "pw123456"})
    assert r.status_code == 202  # no enumeration


@pytest.mark.asyncio
async def test_login_bad_password_returns_401(client):
    r = await client.post(f"{BASE}/login", json={"email": "no@x.com", "password": "bad"})
    assert r.status_code == 401
