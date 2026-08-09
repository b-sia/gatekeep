from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from gatekeep.accounting import calculate_cost, estimate_tokens, log_request
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey, RequestLog


def test_calculate_cost_known_model():
    cost = calculate_cost(
        "claude-sonnet-5", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 12.0


def test_calculate_cost_scales_linearly():
    cost = calculate_cost("claude-sonnet-5", prompt_tokens=500_000, completion_tokens=0)
    assert cost == 1.0


def test_calculate_cost_haiku_alias_is_priced():
    cost = calculate_cost(
        "claude-haiku-4-5-20251001",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost > 0.0


def test_calculate_cost_unknown_model_is_free():
    cost = calculate_cost(
        "llama3", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 0.0


def test_calculate_cost_openai_gpt4o_is_priced():
    cost = calculate_cost(
        "gpt-4o", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost > 0.0


def test_calculate_cost_google_gemini_flash_is_priced():
    cost = calculate_cost(
        "gemini-flash-latest", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 10.5


async def test_log_request_persists_row(session):
    raw = generate_key()
    key = ApiKey(name="c", key_hash=hash_key(raw))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=100,
        completion_tokens=50,
        response_id="chatcmpl-abc",
    )

    found = (
        await session.execute(select(RequestLog).where(RequestLog.id == log.id))
    ).scalar_one()
    assert found.key_id == key.id
    assert found.model == "claude-sonnet-5"
    assert found.prompt_tokens == 100
    assert found.completion_tokens == 50
    assert found.total_tokens == 150
    assert found.cost_usd == calculate_cost("claude-sonnet-5", 100, 50)
    assert found.cached is False
    assert found.cache_key is None
    assert found.response_id == "chatcmpl-abc"
    assert found.created_at is not None


async def test_log_request_can_record_cache_hit(session):
    raw = generate_key()
    key = ApiKey(name="c", key_hash=hash_key(raw))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
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
    key = ApiKey(name="c", key_hash=hash_key(raw))
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=0,
        completion_tokens=0,
        response_id="chatcmpl-semantic-hit",
        cached=True,
        cache_key="semantic",
        cost_usd_override=0.0042,
    )

    assert log.cost_usd == 0.0042
    assert log.cost_usd != calculate_cost("claude-sonnet-5", 0, 0)


async def test_log_request_records_latency_columns(session):
    """Timing kwargs land on the row; omitting them leaves NULLs."""
    key = ApiKey(name="latency", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()

    timed = await log_request(
        session,
        key_id=key.id,
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
    key = ApiKey(name="untimed", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()

    untimed = await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-untimed",
    )
    assert untimed.duration_ms is None
    assert untimed.provider_ms is None
    assert untimed.ttft_ms is None


async def test_log_request_persists_path(session):
    key = ApiKey(name="path-key", key_hash="hash-path")
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-path",
        path="cache_semantic",
    )
    assert log.path == "cache_semantic"


async def test_log_request_path_defaults_to_none(session):
    """A caller with no path available must still be able to log."""
    key = ApiKey(name="no-path-key", key_hash="hash-no-path")
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-no-path",
    )
    assert log.path is None


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_rounds_up_to_at_least_one_token():
    assert estimate_tokens("hi") == 1


def test_estimate_tokens_matches_four_chars_per_token_on_exact_multiples():
    assert estimate_tokens("a" * 8) == 2


def test_estimate_tokens_rounds_up_on_a_partial_final_token():
    assert estimate_tokens("a" * 9) == 3


@pytest_asyncio.fixture
async def key_id(session):
    raw = generate_key()
    key = ApiKey(name="accounting-test", key_hash=hash_key(raw))
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return key.id


async def test_log_request_defaults_outcome_to_ok(session, key_id):
    log = await log_request(
        session,
        key_id=key_id,
        model="claude-sonnet-5",
        prompt_tokens=1,
        completion_tokens=1,
        response_id="resp-outcome-default",
    )
    assert log.outcome == "ok"


async def test_log_request_persists_explicit_outcome(session, key_id):
    log = await log_request(
        session,
        key_id=key_id,
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
