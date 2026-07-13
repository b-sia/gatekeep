from sqlalchemy import select

from gatekeep.accounting import calculate_cost, log_request
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
