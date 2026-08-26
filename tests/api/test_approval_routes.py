import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.accounts import auth_service
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.app import app
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import ApiKey
from tests.helpers import create_account


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _operator_key():
    async with SessionLocal() as s:
        raw = generate_key()
        op = await create_account(s, is_operator=True)
        s.add(ApiKey(name="opkey", key_hash=hash_key(raw), account_id=op.id))
        await s.commit()
    return raw


@pytest.mark.asyncio
async def test_operator_lists_and_approves_pending(client):
    async with SessionLocal() as s:
        pending, tok = await auth_service.signup(s, email="new@x.com", password="pw123456")
        await auth_service.verify_email(s, raw_token=tok)
        await s.commit()
        pid = pending.id
    op = await _operator_key()
    h = {"Authorization": f"Bearer {op}"}
    r = await client.get("/dashboard/api/accounts/pending", headers=h)
    assert r.status_code == 200 and any(a["account_id"] == pid for a in r.json()["accounts"])
    r = await client.post(
        f"/dashboard/api/accounts/{pid}/approve", json={"monthly_budget_usd": 15.0}, headers=h
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"


@pytest.mark.asyncio
async def test_non_operator_cannot_list_pending(client):
    async with SessionLocal() as s:
        raw = generate_key()
        acct = await create_account(s)  # non-operator, approved
        s.add(ApiKey(name="k", key_hash=hash_key(raw), account_id=acct.id))
        await s.commit()
    r = await client.get(
        "/dashboard/api/accounts/pending", headers={"Authorization": f"Bearer {raw}"}
    )
    assert r.status_code == 403
