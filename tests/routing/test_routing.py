from gatekeep.prompts.evals import create_suite
from gatekeep.routing.routing import select_model
from gatekeep.storage.models import EvalRun, Prompt, PromptVersion


async def _suite_with_version(session, prompt_name):
    suite = await create_suite(prompt_name, session, pass_threshold=0.9)
    prompt = Prompt(name=prompt_name)
    session.add(prompt)
    await session.flush()
    version = PromptVersion(prompt_id=prompt.id, version_num=1, template="t", active=True)
    session.add(version)
    await session.flush()
    return suite, version


async def _run(session, suite_id, version_id, model, score, passed):
    session.add(
        EvalRun(
            suite_id=suite_id,
            prompt_version_id=version_id,
            model=model,
            score=score,
            passed=passed,
            report=[],
        )
    )
    await session.commit()


async def test_substitutes_cheaper_passing_model(session):
    suite, version = await _suite_with_version(session, "p")
    # haiku is cheaper than sonnet and has a passing run at 0.95
    await _run(session, suite.id, version.id, "claude-haiku-4-5-20251001", 0.95, True)

    chosen = await select_model("anthropic", "claude-sonnet-5", "p", 0.9, session)
    assert chosen == "claude-haiku-4-5-20251001"


async def test_keeps_requested_when_cheaper_model_below_floor(session):
    suite, version = await _suite_with_version(session, "p")
    await _run(session, suite.id, version.id, "claude-haiku-4-5-20251001", 0.5, False)

    chosen = await select_model("anthropic", "claude-sonnet-5", "p", 0.9, session)
    assert chosen == "claude-sonnet-5"


async def test_keeps_requested_when_no_suite(session):
    chosen = await select_model("anthropic", "claude-sonnet-5", "no-prompt", 0.9, session)
    assert chosen == "claude-sonnet-5"


async def test_never_substitutes_a_model_from_a_different_provider(session):
    """A cheaper model on another provider must never be chosen: the caller
    already resolved a specific provider SDK client for the request, and
    substituting across providers would send the payload to the wrong one."""
    suite, version = await _suite_with_version(session, "p")
    # gpt-4o-mini is far cheaper than claude-sonnet-5 and passes, but it's on
    # a different provider.
    await _run(session, suite.id, version.id, "gpt-4o-mini", 0.95, True)

    chosen = await select_model("anthropic", "claude-sonnet-5", "p", 0.9, session)
    assert chosen == "claude-sonnet-5"


async def test_keeps_requested_when_requested_model_is_unpriced(session):
    """An unpriced requested model has no cost baseline to compare candidates
    against, so routing must not attempt a substitution."""
    suite, _ = await _suite_with_version(session, "p")
    chosen = await select_model("anthropic", "not-a-real-model", "p", 0.9, session)
    assert chosen == "not-a-real-model"
