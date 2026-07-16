from gatekeep.evals import create_suite
from gatekeep.models import EvalRun, Prompt, PromptVersion
from gatekeep.routing import select_model


async def _suite_with_version(session, prompt_name):
    suite = await create_suite(prompt_name, session, pass_threshold=0.9)
    prompt = Prompt(name=prompt_name)
    session.add(prompt)
    await session.flush()
    version = PromptVersion(
        prompt_id=prompt.id, version_num=1, template="t", active=True
    )
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

    chosen = await select_model("claude-sonnet-5", "p", 0.9, session)
    assert chosen == "claude-haiku-4-5-20251001"


async def test_keeps_requested_when_cheaper_model_below_floor(session):
    suite, version = await _suite_with_version(session, "p")
    await _run(session, suite.id, version.id, "claude-haiku-4-5-20251001", 0.5, False)

    chosen = await select_model("claude-sonnet-5", "p", 0.9, session)
    assert chosen == "claude-sonnet-5"


async def test_keeps_requested_when_no_suite(session):
    chosen = await select_model("claude-sonnet-5", "no-prompt", 0.9, session)
    assert chosen == "claude-sonnet-5"
