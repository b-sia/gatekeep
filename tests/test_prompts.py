import asyncio

import pytest
from sqlalchemy import select

from gatekeep.api.openai_schemas import (
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)
from gatekeep.db import SessionLocal
from gatekeep.embeddings import embed_text
from gatekeep.evals import EvalGateFailure, add_case, create_suite
from gatekeep.middleware.cache_exact import (
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.cache_semantic import store_cached_response
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import CachedResponse, Prompt, PromptVersion
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    add_prompt_version,
    clear_candidate_version,
    create_prompt,
    get_active_prompt_version,
    get_prompt,
    list_prompts,
    promote_prompt,
    resolve_prompt_version_for_request,
    rollback_prompt,
    set_candidate_version,
)
from gatekeep.providers.base import CompletionResult
from tests.helpers import create_account


class _FakeProvider:
    """Fake provider that returns queued text responses in order, ignoring the payload."""

    def __init__(self, texts):
        self._texts = list(texts)

    async def complete(self, payload):
        """Pop and return the next queued text as a CompletionResult."""
        return CompletionResult(
            text=self._texts.pop(0), input_tokens=1, output_tokens=1, stop_reason="stop"
        )


def _gate_from(provider):
    """Build an eval gate using `provider` for both generation and judging."""
    from gatekeep.evals import make_eval_gate

    return make_eval_gate(
        provider=provider,
        generate_model="m",
        judge_model="claude-sonnet-5",
        max_tokens=64,
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
        choices=[Choice(index=0, message=ResponseMessage(content="pong"), finish_reason="stop")],
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


# -- Concurrency: promote_prompt's row lock must actually serialize --------
#
# These run two independent SessionLocal() sessions (independent Postgres
# connections) concurrently, which is the only way to observe row-lock
# contention - a single AsyncSession can't race against itself.


async def test_promote_blocks_until_concurrent_holder_of_the_row_lock_commits(session):
    """A second promote_prompt call must block on the Prompt row lock, not race it.

    Regression test for the fix in 177abe6: before it, two concurrent
    promote_prompt calls could both read active_version_id before either
    committed. Here we hold the row lock open on one connection and assert
    a concurrent promote_prompt on another connection is still blocked
    after a generous wait, then only proceeds once the lock is released.
    """
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await session.commit()

    holder = SessionLocal()
    await holder.execute(select(Prompt).where(Prompt.name == "system-context").with_for_update())

    waiter = SessionLocal()
    task = asyncio.create_task(promote_prompt("system-context", 2, waiter))
    try:
        await asyncio.sleep(0.3)
        assert not task.done(), "promote_prompt proceeded without waiting for the row lock"

        await holder.commit()
        promoted = await asyncio.wait_for(task, timeout=2)
        assert promoted.version_num == 2
    finally:
        if not task.done():
            task.cancel()
        await holder.close()
        await waiter.close()


async def test_concurrent_promotes_leave_exactly_one_active_version(session):
    """Many concurrent promote_prompt calls on the same prompt must never
    leave two PromptVersion rows active at once, and active_version_id
    must always point at the row that's actually flagged active."""
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await add_prompt_version("system-context", "v3 text", session)
    await session.commit()

    targets = [2, 3] * 4
    sessions = [SessionLocal() for _ in targets]
    try:
        await asyncio.gather(
            *(
                promote_prompt("system-context", target, s)
                for target, s in zip(targets, sessions, strict=True)
            )
        )
    finally:
        for s in sessions:
            await s.close()

    verify = SessionLocal()
    try:
        versions = (
            (
                await verify.execute(
                    select(PromptVersion)
                    .join(Prompt, Prompt.id == PromptVersion.prompt_id)
                    .where(Prompt.name == "system-context")
                )
            )
            .scalars()
            .all()
        )
        active_versions = [v for v in versions if v.active]
        assert len(active_versions) == 1

        prompt_row = (
            await verify.execute(select(Prompt).where(Prompt.name == "system-context"))
        ).scalar_one()
        assert prompt_row.active_version_id == active_versions[0].id
    finally:
        await verify.close()


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
    account = await create_account(session)
    redis = get_redis()
    h = hash_request({"model": "m", "messages": [], "max_tokens": 1})
    await set_cached_response(
        redis, account.id, h, _response(), ttl_seconds=60, prompt_name="system-context"
    )

    await promote_prompt("system-context", 2, session, redis=redis)

    assert await get_cached_response(redis, account.id, h) is None


async def test_promote_invalidates_semantic_cache_rows_tagged_with_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    account = await create_account(session)
    embedding = embed_text("hello")
    await store_cached_response(
        session,
        account_id=account.id,
        exact_hash="tagged-hash",
        user_messages_text="hello",
        embedding=embedding,
        response_text="hi",
        model="m",
        cost_usd=0.001,
        max_tokens=1000,
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
    account = await create_account(session)
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
        redis, account.id, h_a, _response("a"), ttl_seconds=60, prompt_name="a"
    )
    await set_cached_response(
        redis, account.id, h_b, _response("b"), ttl_seconds=60, prompt_name="b"
    )
    await set_cached_response(redis, account.id, h_plain, _response("plain"), ttl_seconds=60)

    embedding = embed_text("x")
    await store_cached_response(
        session,
        account_id=account.id,
        exact_hash="b-hash",
        user_messages_text="x",
        embedding=embedding,
        response_text="y",
        model="m",
        cost_usd=0.001,
        max_tokens=1000,
        prompt_name="b",
    )
    await store_cached_response(
        session,
        account_id=account.id,
        exact_hash="plain-hash",
        user_messages_text="x2",
        embedding=embedding,
        response_text="y2",
        model="m",
        cost_usd=0.001,
        max_tokens=1000,
    )

    await promote_prompt("a", 2, session, redis=redis)

    assert await get_cached_response(redis, account.id, h_a) is None
    assert await get_cached_response(redis, account.id, h_b) is not None
    assert await get_cached_response(redis, account.id, h_plain) is not None

    rows = (await session.execute(select(CachedResponse))).scalars().all()
    hashes = {r.exact_hash for r in rows}
    assert hashes == {"b-hash", "plain-hash"}


async def test_promote_is_noop_when_nothing_cached_for_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    redis = get_redis()

    promoted = await promote_prompt("system-context", 2, session, redis=redis)
    assert promoted.version_num == 2


async def test_promote_blocked_when_eval_gate_fails(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact",
        expected="pong",
    )

    with pytest.raises(EvalGateFailure):
        await promote_prompt(
            "system-context", 2, session, gate=_gate_from(_FakeProvider(["wrong"]))
        )

    # active version unchanged (still v1)
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 1


async def test_promote_allowed_when_eval_gate_passes(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="pong",
    )

    promoted = await promote_prompt(
        "system-context", 2, session, gate=_gate_from(_FakeProvider(["...pong..."]))
    )
    assert promoted.version_num == 2


async def test_promote_allowed_when_no_suite_registered(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)

    promoted = await promote_prompt(
        "system-context", 2, session, gate=_gate_from(_FakeProvider([]))
    )
    assert promoted.version_num == 2


async def test_rollback_invalidates_cache_for_the_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)
    account = await create_account(session)
    redis = get_redis()
    h = hash_request({"model": "m", "messages": [], "max_tokens": 1})
    await set_cached_response(
        redis, account.id, h, _response(), ttl_seconds=60, prompt_name="system-context"
    )

    await rollback_prompt("system-context", session, redis=redis)

    assert await get_cached_response(redis, account.id, h) is None


# -- A/B candidate: set_candidate_version / clear_candidate_version --------


async def test_set_candidate_version_configures_prompt(session):
    await create_prompt("system-context", "v1", session)
    v2 = await add_prompt_version("system-context", "v2 text", session)

    prompt = await set_candidate_version("system-context", 2, 10.0, session)

    assert prompt.candidate_version_id == v2.id
    assert prompt.candidate_traffic_pct == 10.0
    # setting a candidate must not touch which version is active
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 1


async def test_set_candidate_version_rejects_out_of_range_pct(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)

    with pytest.raises(ValueError):
        await set_candidate_version("system-context", 2, 101.0, session)
    with pytest.raises(ValueError):
        await set_candidate_version("system-context", 2, -1.0, session)


async def test_set_candidate_version_raises_for_unknown_prompt(session):
    with pytest.raises(PromptNotFoundError):
        await set_candidate_version("does-not-exist", 1, 10.0, session)


async def test_set_candidate_version_raises_for_unknown_version(session):
    await create_prompt("system-context", "v1", session)
    with pytest.raises(PromptVersionNotFoundError):
        await set_candidate_version("system-context", 99, 10.0, session)


async def test_set_candidate_version_replaces_previous_candidate(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    v3 = await add_prompt_version("system-context", "v3 text", session)

    await set_candidate_version("system-context", 2, 10.0, session)
    prompt = await set_candidate_version("system-context", 3, 50.0, session)

    assert prompt.candidate_version_id == v3.id
    assert prompt.candidate_traffic_pct == 50.0


async def test_clear_candidate_version_resets_fields(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await set_candidate_version("system-context", 2, 10.0, session)

    prompt = await clear_candidate_version("system-context", session)

    assert prompt.candidate_version_id is None
    assert prompt.candidate_traffic_pct is None


async def test_clear_candidate_version_is_noop_when_none_configured(session):
    await create_prompt("system-context", "v1", session)

    prompt = await clear_candidate_version("system-context", session)

    assert prompt.candidate_version_id is None


async def test_clear_candidate_version_raises_for_unknown_prompt(session):
    with pytest.raises(PromptNotFoundError):
        await clear_candidate_version("does-not-exist", session)


# -- A/B candidate: resolve_prompt_version_for_request ----------------------


async def test_resolve_returns_active_version_when_no_candidate_configured(session):
    await create_prompt("system-context", "v1", session)

    version = await resolve_prompt_version_for_request("system-context", session)

    assert version.version_num == 1


async def test_resolve_matches_get_active_prompt_version_when_unset(session):
    """Unset candidate must behave exactly like today's plain active-version
    resolution - this is the "no split = today's behavior" invariant."""
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)

    resolved = await resolve_prompt_version_for_request("system-context", session)
    active = await get_active_prompt_version("system-context", session)

    assert resolved.version_num == active.version_num == 2


async def test_resolve_raises_for_unknown_prompt(session):
    with pytest.raises(PromptNotFoundError):
        await resolve_prompt_version_for_request("does-not-exist", session)


async def test_resolve_with_zero_pct_always_returns_active(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await set_candidate_version("system-context", 2, 0.0, session)

    for _ in range(50):
        version = await resolve_prompt_version_for_request("system-context", session)
        assert version.version_num == 1


async def test_resolve_with_hundred_pct_always_returns_candidate(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await set_candidate_version("system-context", 2, 100.0, session)

    for _ in range(50):
        version = await resolve_prompt_version_for_request("system-context", session)
        assert version.version_num == 2


async def test_resolve_distributes_traffic_near_configured_percentage(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await set_candidate_version("system-context", 2, 30.0, session)

    n = 2000
    candidate_hits = 0
    for _ in range(n):
        version = await resolve_prompt_version_for_request("system-context", session)
        if version.version_num == 2:
            candidate_hits += 1

    ratio = candidate_hits / n
    # generous tolerance to keep this non-flaky while still proving the
    # split is roughly honored, not e.g. inverted or ignored
    assert 0.20 < ratio < 0.40


# -- A/B candidate must not disturb promote_prompt / rollback_prompt -------


async def test_promote_unaffected_by_inflight_candidate(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    v3 = await add_prompt_version("system-context", "v3 text", session)
    await set_candidate_version("system-context", 3, 20.0, session)

    promoted = await promote_prompt("system-context", 2, session)

    assert promoted.version_num == 2
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 2
    # the candidate configuration itself is untouched by promotion
    prompt_row = next(p for p in await list_prompts(session) if p.name == "system-context")
    assert prompt_row.candidate_version_id == v3.id
    assert prompt_row.candidate_traffic_pct == 20.0


async def test_rollback_unaffected_by_inflight_candidate(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await promote_prompt("system-context", 2, session)
    await add_prompt_version("system-context", "v3 text", session)
    await set_candidate_version("system-context", 3, 20.0, session)

    rolled_back = await rollback_prompt("system-context", session)

    assert rolled_back.version_num == 1
    template = await get_prompt("system-context", session)
    assert template == "v1"


async def test_setting_candidate_does_not_invalidate_cache(session):
    """Setting/clearing a candidate is a lighter-weight operation than
    promote_prompt: it must not invalidate cached responses, since the
    active version (what most traffic still gets) hasn't changed."""
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    account = await create_account(session)
    redis = get_redis()
    h = hash_request({"model": "m", "messages": [], "max_tokens": 1})
    await set_cached_response(
        redis, account.id, h, _response(), ttl_seconds=60, prompt_name="system-context"
    )

    await set_candidate_version("system-context", 2, 10.0, session)
    assert await get_cached_response(redis, account.id, h) is not None

    await clear_candidate_version("system-context", session)
    assert await get_cached_response(redis, account.id, h) is not None
