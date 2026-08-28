import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.accounts import auth_service
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as c:
        yield c


async def _approved_login(client):
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="s@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=raw)
        account.status = "approved"
        await s.commit()
        acct_id = account.id
    r = await client.post(
        "/dashboard/api/auth/login", json={"email": "s@x.com", "password": "pw123456"}
    )
    return acct_id, r.json()["csrf_token"]


@pytest.mark.asyncio
async def test_me_works_with_session_cookie(client):
    acct_id, _ = await _approved_login(client)
    r = await client.get("/dashboard/api/me")
    assert r.status_code == 200 and r.json()["account_id"] == acct_id


@pytest.mark.asyncio
async def test_pending_session_blocked_from_minting_key(client):
    async with SessionLocal() as s:
        account, raw = await auth_service.signup(s, email="pend@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=raw)
        await s.commit()
        acct_id = account.id
    login = await client.post(
        "/dashboard/api/auth/login", json={"email": "pend@x.com", "password": "pw123456"}
    )
    csrf = login.json()["csrf_token"]
    r = await client.post(
        f"/dashboard/api/accounts/{acct_id}/keys",
        json={"name": "k1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 403  # pending -> require_approved blocks


@pytest.mark.asyncio
async def test_approved_session_can_mint_own_key(client):
    acct_id, csrf = await _approved_login(client)
    r = await client.post(
        f"/dashboard/api/accounts/{acct_id}/keys",
        json={"name": "k1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200 and r.json()["key"].startswith("gk-")
