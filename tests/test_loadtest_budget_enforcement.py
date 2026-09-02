from __future__ import annotations

import pytest
from sqlalchemy import func, select

import gatekeep.app as app_module
from gatekeep.accounts import accounting
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.middleware.budget import get_period_spend
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.providers.stub import StubProvider
from gatekeep.storage.models import ApiKey, RequestLog
from tests.helpers import create_account


@pytest.fixture(autouse=True)
async def _clean_budget_keys():
    """Flush any leftover budget:* keys so this test starts from a clean slate.

    `_create_schema` (tests/conftest.py) drops and recreates the whole DB
    schema per test, so a fresh account here can get the same id (e.g. 1) as
    one from an earlier test - but nothing else flushes the `budget:*` Redis
    namespace between tests (see the identical fixture in
    tests/middleware/test_budget.py), so a stale spend counter from a prior
    test can otherwise leak in and make this test's exact-request-count
    assertion flaky depending on run order/history.
    """
    redis = get_redis()
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("budget:*"):
        await redis.delete(key)


async def test_stub_budget_enforcement_blocks_at_predicted_spend(client, session, monkeypatch):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setitem(app_module._providers, "stub", StubProvider())
    # Inflate the fixed stub price so a tiny budget trips in a handful of
    # requests instead of needing an enormous output-token count.
    monkeypatch.setattr(accounting, "STUB_PRICE_PER_1M", 1000.0)

    # cost/request = (prompt_tokens + completion_tokens) / 1e6 * 1000
    #   prompt_tokens == 2: StubProvider._payload_text joins the (empty)
    #   system string and the "ping N" message with "\n", giving "\nping N" (7
    #   chars, since "ping 0".."ping 4" are all 6 chars) - estimate_tokens
    #   rounds that up to ceil(7/4) == 2.
    #   completion_tokens == 10 (stub/lat1-out10)
    #   => (2 + 10) / 1_000_000 * 1000 == 0.012 per request
    # budget 0.032 allows exactly 3 requests (0.036 after the 3rd is already
    # >= budget) before the 4th is blocked.
    #
    # The prompt varies per iteration (f"ping {i}") so every request hashes
    # differently for gatekeep's exact-match response cache
    # (middleware/cache_exact.py). A cache hit deliberately contributes $0 to
    # the budget counter (see accounting.log_request's docstring), so five
    # identical bodies would only ever debit the budget once (request 1) and
    # the cap would never trip - confirmed by reproducing that failure mode
    # before adding this variation.
    account = await create_account(session, monthly_budget_usd=0.032)
    raw = generate_key()
    session.add(ApiKey(name="k", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()

    statuses = []
    for i in range(5):
        body = {
            "model": "stub/lat1-out10",
            "messages": [{"role": "user", "content": f"ping {i}"}],
        }
        r = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {raw}"}, json=body
        )
        statuses.append(r.status_code)

    assert statuses == [200, 200, 200, 429, 429]

    spend_from_db = (
        await session.execute(
            select(func.sum(RequestLog.cost_usd)).where(RequestLog.account_id == account.id)
        )
    ).scalar_one()
    assert spend_from_db == pytest.approx(0.036)

    redis = get_redis()
    counter_spend = await get_period_spend(session, redis, account_id=account.id)
    assert counter_spend == pytest.approx(spend_from_db)
