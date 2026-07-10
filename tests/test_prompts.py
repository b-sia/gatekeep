import pytest
from sqlalchemy import select

from gatekeep.api.openai_schemas import (
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gatekeep.embeddings import embed_text
from gatekeep.middleware.cache_exact import (
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.cache_semantic import store_cached_response
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import CachedResponse
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    get_prompt,
    list_prompts,
    promote_prompt,
    rollback_prompt,
)


@pytest.fixture(autouse=True)
async def _clean_cache():
    """Flush any leftover exact-cache keys so invalidation tests start clean."""
    redis = get_redis()
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)
    yield
    async for key in redis.scan_iter("cache:exact:*"):
        await redis.delete(key)


def _response(id="chatcmpl-1"):
    """Build a minimal ChatCompletionResponse for cache round-trip tests."""
    return ChatCompletionResponse(
        id=id,
        created=1234,
        model="claude-sonnet-5",
        choices=[
            Choice(
                index=0, message=ResponseMessage(content="pong"), finish_reason="stop"
            )
        ],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )


async def test_create_prompt_makes_version_1_active(session):
    prompt = await create_prompt("system-context", "hello {name}", session)
    assert prompt.name == "system-context"

    template = await get_prompt("system-context", session)
    assert template == "hello {name}"

    version = await get_active_prompt_version("system-context", session)
    assert version.version_num == 1
    assert version.active is True


async def test_create_prompt_rejects_duplicate_name(session):
    await create_prompt("system-context", "v1", session)
    with pytest.raises(ValueError):
        await create_prompt("system-context", "v1-again", session)


async def test_get_prompt_raises_for_unknown_name(session):
    with pytest.raises(PromptNotFoundError):
        await get_prompt("does-not-exist", session)


async def test_add_prompt_version_increments_per_prompt(session):
    await create_prompt("system-context", "v1", session)
    v2 = await add_prompt_version("system-context", "v2 text", session)
    assert v2.version_num == 2
    assert v2.active is False

    # active version is still v1 until explicitly promoted
    template = await get_prompt("system-context", session)
    assert template == "v1"


async def test_add_prompt_version_per_prompt_numbering_is_independent(session):
    await create_prompt("a", "a1", session)
    await create_prompt("b", "b1", session)
    v2_a = await add_prompt_version("a", "a2", session)
    assert v2_a.version_num == 2

    v2_b = await add_prompt_version("b", "b2", session)
    assert v2_b.version_num == 2


async def test_promote_switches_active_version_and_flips_active_flags(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)

    promoted = await promote_prompt("system-context", 2, session)
    assert promoted.version_num == 2
    assert promoted.active is True

    template = await get_prompt("system-context", session)
    assert template == "v2 text"

    v1 = await get_active_prompt_version("system-context", session)
    assert v1.version_num == 2  # active version is now v2

    all_versions = await list_prompts(session)
    prompt_row = next(p for p in all_versions if p.name == "system-context")
    assert prompt_row.active_version_id == promoted.id


async def test_promote_raises_for_unknown_version(session):
    await create_prompt("system-context", "v1", session)
    with pytest.raises(PromptVersionNotFoundError):
        await promote_prompt("system-context", 99, session)


async def test_promote_raises_for_unknown_prompt(session):
    with pytest.raises(PromptNotFoundError):
        await promote_prompt("does-not-exist", 1, session)


async def test_rollback_reverts_to_previously_active_version(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)

    rolled_back = await rollback_prompt("system-context", session)
    assert rolled_back.version_num == 1

    template = await get_prompt("system-context", session)
    assert template == "v1"


async def test_rollback_raises_when_no_previous_version_recorded(session):
    await create_prompt("system-context", "v1", session)
    with pytest.raises(ValueError):
        await rollback_prompt("system-context", session)


async def test_rollback_skips_drafted_but_never_promoted_version(session):
    # create v1 (active) -> add-version v2 (never promoted) -> add-version v3
    # -> promote v3 -> rollback should go to v1 (the last *actually active*
    # version), NOT v2, which was drafted but never live.
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await add_prompt_version("system-context", "v3 text", session)
    await promote_prompt("system-context", 3, session)

    rolled_back = await rollback_prompt("system-context", session)
    assert rolled_back.version_num == 1

    template = await get_prompt("system-context", session)
    assert template == "v1"


async def test_rollback_twice_toggles_between_last_two_active_versions(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)

    first_rollback = await rollback_prompt("system-context", session)
    assert first_rollback.version_num == 1

    second_rollback = await rollback_prompt("system-context", session)
    assert second_rollback.version_num == 2


async def test_list_prompts_returns_all_prompts(session):
    await create_prompt("a", "a1", session)
    await create_prompt("b", "b1", session)

    prompts = await list_prompts(session)
    names = {p.name for p in prompts}
    assert names == {"a", "b"}


# -- promote_prompt invalidates cache entries built from the old version ----


async def test_promote_invalidates_exact_cache_entries_tagged_with_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    redis = get_redis()
    h = hash_request({"model": "m", "messages": [], "max_tokens": 1})
    await set_cached_response(
        redis, h, _response(), ttl_seconds=60, prompt_name="system-context"
    )

    await promote_prompt("system-context", 2, session, redis=redis)

    assert await get_cached_response(redis, h) is None


async def test_promote_invalidates_semantic_cache_rows_tagged_with_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    embedding = embed_text("hello")
    await store_cached_response(
        session,
        exact_hash="tagged-hash",
        user_messages_text="hello",
        embedding=embedding,
        response_text="hi",
        model="m",
        cost_usd=0.001,
        prompt_name="system-context",
    )

    await promote_prompt("system-context", 2, session)

    rows = (
        (
            await session.execute(
                select(CachedResponse).where(CachedResponse.exact_hash == "tagged-hash")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_promote_leaves_other_prompts_and_untagged_cache_entries_untouched(
    session,
):
    await create_prompt("a", "a1", session)
    await add_prompt_version("a", "a2 text", session)
    await create_prompt("b", "b1", session)
    redis = get_redis()

    h_a = hash_request(
        {"model": "m", "messages": [{"role": "user", "content": "a"}], "max_tokens": 1}
    )
    h_b = hash_request(
        {"model": "m", "messages": [{"role": "user", "content": "b"}], "max_tokens": 1}
    )
    h_plain = hash_request(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "plain"}],
            "max_tokens": 1,
        }
    )
    await set_cached_response(
        redis, h_a, _response("a"), ttl_seconds=60, prompt_name="a"
    )
    await set_cached_response(
        redis, h_b, _response("b"), ttl_seconds=60, prompt_name="b"
    )
    await set_cached_response(redis, h_plain, _response("plain"), ttl_seconds=60)

    embedding = embed_text("x")
    await store_cached_response(
        session,
        exact_hash="b-hash",
        user_messages_text="x",
        embedding=embedding,
        response_text="y",
        model="m",
        cost_usd=0.001,
        prompt_name="b",
    )
    await store_cached_response(
        session,
        exact_hash="plain-hash",
        user_messages_text="x2",
        embedding=embedding,
        response_text="y2",
        model="m",
        cost_usd=0.001,
    )

    await promote_prompt("a", 2, session, redis=redis)

    assert await get_cached_response(redis, h_a) is None
    assert await get_cached_response(redis, h_b) is not None
    assert await get_cached_response(redis, h_plain) is not None

    rows = (await session.execute(select(CachedResponse))).scalars().all()
    hashes = {r.exact_hash for r in rows}
    assert hashes == {"b-hash", "plain-hash"}


async def test_promote_is_noop_when_nothing_cached_for_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    redis = get_redis()

    promoted = await promote_prompt("system-context", 2, session, redis=redis)
    assert promoted.version_num == 2


async def test_rollback_invalidates_cache_for_the_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)
    redis = get_redis()
    h = hash_request({"model": "m", "messages": [], "max_tokens": 1})
    await set_cached_response(
        redis, h, _response(), ttl_seconds=60, prompt_name="system-context"
    )

    await rollback_prompt("system-context", session, redis=redis)

    assert await get_cached_response(redis, h) is None
