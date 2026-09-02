from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from gatekeep.accounts.accounting import (
    calculate_cost,
    enforce_pricing_policy,
    estimate_tokens,
    log_request,
)
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.observability.metrics import unpriced_model_total
from gatekeep.storage.models import ApiKey, RequestLog
from tests.helpers import create_account, create_key


@pytest.fixture
def miss_policy(monkeypatch):
    """Set `pricing_miss_policy` (and optionally the ceiling) for one test,
    clearing the cached Settings before and after so the change is isolated."""

    def _set(policy: str, *, ceiling: float | None = None) -> None:
        monkeypatch.setenv("PRICING_MISS_POLICY", policy)
        if ceiling is not None:
            monkeypatch.setenv("PRICING_CEILING_PER_1M", str(ceiling))
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("provider", "model", "prompt_tokens", "completion_tokens", "expected"),
    [
        # known model, priced exactly
        ("anthropic", "claude-sonnet-5", 1_000_000, 1_000_000, 12.0),
        # cost scales linearly with token counts
        ("anthropic", "claude-sonnet-5", 500_000, 0, 1.0),
        # unknown Ollama model is free (never billed)
        ("ollama", "llama3", 1_000_000, 1_000_000, 0.0),
        # calculate_cost is numeric-only: under the default "reject" policy it
        # still returns $0 for an unpriced paid model (the request never reaches
        # here, since enforce_pricing_policy refuses it first).
        ("anthropic", "not-a-real-model", 1_000_000, 1_000_000, 0.0),
        ("google", "gemini-flash-latest", 1_000_000, 1_000_000, 10.5),
    ],
)
def test_calculate_cost_exact(provider, model, prompt_tokens, completion_tokens, expected):
    cost = calculate_cost(
        provider, model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    assert cost == expected


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("anthropic", "claude-haiku-4-5-20251001"),  # haiku alias is priced
        ("openai", "gpt-4o"),
    ],
)
def test_calculate_cost_priced_model_is_positive(provider, model):
    cost = calculate_cost(provider, model, prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost > 0.0


def test_calculate_cost_unpriced_paid_model_uses_ceiling_under_ceiling_policy(miss_policy):
    miss_policy("ceiling", ceiling=100.0)
    cost = calculate_cost(
        "anthropic", "not-a-real-model", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 200.0  # $100/1M input + $100/1M output


def test_calculate_cost_unpriced_ollama_model_is_free_even_under_ceiling(miss_policy):
    """Ollama is never billed, so the ceiling policy must not touch it."""
    miss_policy("ceiling", ceiling=100.0)
    cost = calculate_cost("ollama", "llama3", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == 0.0


def test_calculate_cost_unpriced_paid_model_is_zero_under_alert_zero_policy(miss_policy):
    miss_policy("alert_zero")
    cost = calculate_cost(
        "anthropic", "not-a-real-model", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 0.0


# --- enforce_pricing_policy ---------------------------------------------------


def test_enforce_pricing_policy_rejects_unpriced_paid_model_by_default(miss_policy):
    miss_policy("reject")
    rejection = enforce_pricing_policy("anthropic", "not-a-real-model")
    assert rejection is not None
    assert "not-a-real-model" in rejection


def test_enforce_pricing_policy_allows_priced_model():
    assert enforce_pricing_policy("anthropic", "claude-sonnet-5") is None


def test_enforce_pricing_policy_never_rejects_ollama(miss_policy):
    """Even under "reject", a self-hosted Ollama model is served."""
    miss_policy("reject")
    assert enforce_pricing_policy("ollama", "llama3-local") is None


def test_enforce_pricing_policy_ceiling_and_alert_zero_do_not_reject(miss_policy):
    miss_policy("ceiling", ceiling=100.0)
    assert enforce_pricing_policy("anthropic", "not-a-real-model") is None
    miss_policy("alert_zero")
    assert enforce_pricing_policy("anthropic", "not-a-real-model") is None


def test_calculate_cost_is_provider_scoped():
    """A model priced under one provider does not leak into another provider's lookup."""
    cost = calculate_cost(
        "openai", "claude-sonnet-5", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 0.0


async def test_log_request_persists_row(session):
    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=100,
        completion_tokens=50,
        response_id="chatcmpl-abc",
    )

    found = (await session.execute(select(RequestLog).where(RequestLog.id == log.id))).scalar_one()
    assert found.key_id == key.id
    assert found.provider == "anthropic"
    assert found.model == "claude-sonnet-5"
    assert found.prompt_tokens == 100
    assert found.completion_tokens == 50
    assert found.total_tokens == 150
    assert found.cost_usd == calculate_cost("anthropic", "claude-sonnet-5", 100, 50)
    assert found.cached is False
    assert found.cache_key is None
    assert found.response_id == "chatcmpl-abc"
    assert found.created_at is not None


async def test_log_request_stamps_account_id(session):
    """The row is denormalized with the caller's account_id."""
    account = await create_account(session)
    key = await create_key(session, account, key_hash="acct-log")
    await session.commit()

    await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-1",
    )
    row = (await session.execute(select(RequestLog))).scalar_one()
    assert row.account_id == account.id
    assert row.key_id == key.id


async def test_log_request_can_record_cache_hit(session):
    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=0,
        response_id="chatcmpl-cached",
        cached=True,
        cache_key="abc123",
    )
    assert log.cached is True
    assert log.cache_key == "abc123"


async def test_log_request_cost_usd_override_is_used_instead_of_calculated_cost(
    session,
):
    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="c", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=0,
        completion_tokens=0,
        response_id="chatcmpl-semantic-hit",
        cached=True,
        cache_key="semantic",
        cost_usd_override=0.0042,
    )

    assert log.cost_usd == 0.0042
    assert log.cost_usd != calculate_cost("anthropic", "claude-sonnet-5", 0, 0)


async def test_log_request_records_latency_columns(session):
    """Timing kwargs land on the row; omitting them leaves NULLs."""
    account = await create_account(session)
    key = ApiKey(name="latency", key_hash=hash_key(generate_key()), account_id=account.id)
    session.add(key)
    await session.commit()

    timed = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-timed",
        duration_ms=1234.5,
        provider_ms=1200.0,
        ttft_ms=300.0,
    )
    assert timed.duration_ms == pytest.approx(1234.5)
    assert timed.provider_ms == pytest.approx(1200.0)
    assert timed.ttft_ms == pytest.approx(300.0)


async def test_log_request_latency_columns_default_to_none(session):
    """A caller with no timing available must still be able to log."""
    account = await create_account(session)
    key = ApiKey(name="untimed", key_hash=hash_key(generate_key()), account_id=account.id)
    session.add(key)
    await session.commit()

    untimed = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-untimed",
    )
    assert untimed.duration_ms is None
    assert untimed.provider_ms is None
    assert untimed.ttft_ms is None


async def test_log_request_persists_path(session):
    account = await create_account(session)
    key = ApiKey(name="path-key", key_hash="hash-path", account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="openai",
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-path",
        path="cache_semantic",
    )
    assert log.path == "cache_semantic"


async def test_log_request_path_defaults_to_none(session):
    """A caller with no path available must still be able to log."""
    account = await create_account(session)
    key = ApiKey(name="no-path-key", key_hash="hash-no-path", account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        account_id=account.id,
        provider="openai",
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-no-path",
    )
    assert log.path is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),  # empty string is zero
        ("hi", 1),  # rounds up to at least one token
        ("a" * 8, 2),  # exact multiple of 4 chars/token
        ("a" * 9, 3),  # rounds up on a partial final token
    ],
)
def test_estimate_tokens(text, expected):
    assert estimate_tokens(text) == expected


@pytest_asyncio.fixture
async def key_and_account_id(session):
    """Return a (key_id, account_id) pair for a freshly created key and account."""
    raw = generate_key()
    account = await create_account(session)
    key = ApiKey(name="accounting-test", key_hash=hash_key(raw), account_id=account.id)
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key.id, account.id


async def test_log_request_defaults_outcome_to_ok(session, key_and_account_id):
    """`outcome` defaults to "ok" when the caller doesn't pass one."""
    key_id, account_id = key_and_account_id
    log = await log_request(
        session,
        key_id=key_id,
        account_id=account_id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1,
        completion_tokens=1,
        response_id="resp-outcome-default",
    )
    assert log.outcome == "ok"


async def test_log_request_persists_explicit_outcome(session, key_and_account_id):
    """An explicit `outcome` value (e.g. "provider_error") is stored as-is."""
    key_id, account_id = key_and_account_id
    log = await log_request(
        session,
        key_id=key_id,
        account_id=account_id,
        provider="anthropic",
        model="claude-sonnet-5",
        prompt_tokens=1,
        completion_tokens=1,
        response_id="resp-outcome-explicit",
        outcome="provider_error",
    )
    await session.refresh(log)
    fetched = (
        await session.execute(select(RequestLog).where(RequestLog.id == log.id))
    ).scalar_one()
    assert fetched.outcome == "provider_error"


# --- stub billing ---------------------------------------------------------------


@pytest.fixture
def stub_enabled(monkeypatch):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_calculate_cost_stub_is_billed_at_the_fixed_price_when_enabled(stub_enabled):
    cost = calculate_cost(
        "stub", "lat50-out200", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 2.0  # $1/1M input + $1/1M output at the default STUB_PRICE_PER_1M


def test_calculate_cost_stub_is_free_when_flag_disabled():
    cost = calculate_cost(
        "stub", "lat50-out200", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 0.0


def test_enforce_pricing_policy_never_rejects_stub_regardless_of_miss_policy(
    stub_enabled, miss_policy
):
    miss_policy("reject")
    assert enforce_pricing_policy("stub", "lat50-out200-itl5") is None


def test_enforce_pricing_policy_stub_never_increments_unpriced_metric(stub_enabled):
    before = unpriced_model_total.labels(provider="stub", outcome="rejected")._value.get()
    enforce_pricing_policy("stub", "lat50-out200")
    after = unpriced_model_total.labels(provider="stub", outcome="rejected")._value.get()
    assert after == before
