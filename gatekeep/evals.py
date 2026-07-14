from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import EvalCase, EvalRun, EvalSuite, PromptVersion
from gatekeep.prompts import (
    PromptNotFoundError,
    get_active_prompt_version,
    _get_prompt_row,
)

_JUDGE_TEMPLATE = (
    "Given criteria: {criteria}\n\n"
    "Output: {actual}\n\n"
    "Does the output satisfy the criteria? Answer PASS or FAIL and one sentence why."
)

Gate = Callable[[str, PromptVersion, AsyncSession], Awaitable[None]]


class EvalGateFailure(Exception):
    """Raised to block a promotion when a prompt version fails its eval suite.

    Carries the persisted EvalRun so callers (the CLI) can print the report
    without re-running the suite.
    """

    def __init__(self, eval_run: EvalRun) -> None:
        self.eval_run = eval_run
        super().__init__(
            f"eval gate failed: score {eval_run.score:.2f} < threshold "
            f"(run id {eval_run.id})"
        )


async def create_suite(
    prompt_name: str,
    session: AsyncSession,
    *,
    pass_threshold: float,
    name: str | None = None,
) -> EvalSuite:
    """Create an eval suite bound to `prompt_name` (one suite per prompt).

    `name` defaults to `prompt_name`. Raises sqlalchemy IntegrityError if a
    suite already exists for this prompt (prompt_name is unique).
    """
    suite = EvalSuite(
        name=name or prompt_name,
        prompt_name=prompt_name,
        pass_threshold=pass_threshold,
    )
    session.add(suite)
    await session.commit()
    await session.refresh(suite)
    return suite


async def get_suite_for_prompt(
    prompt_name: str, session: AsyncSession
) -> EvalSuite | None:
    """Return the eval suite bound to `prompt_name`, or None if none is registered."""
    return (
        await session.execute(
            select(EvalSuite).where(EvalSuite.prompt_name == prompt_name)
        )
    ).scalar_one_or_none()


async def add_case(
    suite_id: int,
    session: AsyncSession,
    *,
    input_messages: list[dict],
    check_type: str,
    expected: str | None = None,
    judge_criteria: str | None = None,
    reviewed: bool = True,
    source: str = "manual",
) -> EvalCase:
    """Add one case to a suite.

    Raises ValueError if the check_type/argument combination is invalid:
    `exact`/`contains` require `expected`; `llm_judge` requires `judge_criteria`.
    """
    if check_type in ("exact", "contains") and expected is None:
        raise ValueError(f"check_type {check_type!r} requires `expected`")
    if check_type == "llm_judge" and judge_criteria is None:
        raise ValueError("check_type 'llm_judge' requires `judge_criteria`")
    if check_type not in ("exact", "contains", "llm_judge"):
        raise ValueError(f"unknown check_type {check_type!r}")
    case = EvalCase(
        suite_id=suite_id,
        input_messages=input_messages,
        check_type=check_type,
        expected=expected,
        judge_criteria=judge_criteria,
        reviewed=reviewed,
        source=source,
    )
    session.add(case)
    await session.commit()
    await session.refresh(case)
    return case


async def _score_case(
    case: EvalCase,
    template: str,
    *,
    provider,
    generate_model: str,
    judge_model: str,
    max_tokens: int,
) -> dict:
    """Run one case and return its report dict {case_id, passed, actual_output, reason}."""
    payload = {
        "model": generate_model,
        "messages": case.input_messages,
        "max_tokens": max_tokens,
    }
    if template:
        payload["system"] = template
    actual = (await provider.complete(payload)).text

    if case.check_type == "exact":
        passed = actual == case.expected
        reason = "exact match" if passed else "did not match expected"
    elif case.check_type == "contains":
        passed = case.expected in actual
        reason = "substring found" if passed else "expected substring absent"
    else:  # llm_judge, uses the fixed stronger judge model (decided Q2)
        judge_payload = {
            "model": judge_model,
            "messages": [
                {
                    "role": "user",
                    "content": _JUDGE_TEMPLATE.format(
                        criteria=case.judge_criteria, actual=actual
                    ),
                }
            ],
            "max_tokens": 128,
        }
        verdict = (await provider.complete(judge_payload)).text
        passed = verdict.strip().upper().startswith("PASS")
        reason = verdict.strip()

    return {
        "case_id": case.id,
        "passed": passed,
        "actual_output": actual,
        "reason": reason,
    }


async def run_eval_suite(
    suite: EvalSuite,
    prompt_version: PromptVersion,
    session: AsyncSession,
    *,
    provider,
    generate_model: str,
    judge_model: str,
    max_tokens: int,
    include_unreviewed: bool = False,
) -> EvalRun:
    """Render `prompt_version` against every case in `suite`, score, and persist an EvalRun.

    By default only reviewed cases run; pass include_unreviewed=True to also
    run curated-but-unreviewed cases. Score is the fraction of cases that
    pass; an empty case set scores 1.0 (vacuously passes). Persists one
    EvalRun with the full per-case report so a failed gate leaves a paper
    trail without re-running.
    """
    query = select(EvalCase).where(EvalCase.suite_id == suite.id)
    if not include_unreviewed:
        query = query.where(EvalCase.reviewed.is_(True))
    cases = list((await session.execute(query)).scalars().all())

    report: list[dict] = []
    for case in cases:
        report.append(
            await _score_case(
                case,
                prompt_version.template,
                provider=provider,
                generate_model=generate_model,
                judge_model=judge_model,
                max_tokens=max_tokens,
            )
        )

    passed_count = sum(1 for r in report if r["passed"])
    score = passed_count / len(report) if report else 1.0
    run = EvalRun(
        suite_id=suite.id,
        prompt_version_id=prompt_version.id,
        model=generate_model,
        score=score,
        passed=score >= suite.pass_threshold,
        report=report,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


def make_eval_gate(
    *, provider, generate_model: str, judge_model: str, max_tokens: int
) -> Gate:
    """Build a promotion gate that runs the prompt's suite and blocks on failure.

    The returned coroutine is a no-op when no suite is registered for the
    prompt (opt-in gate). On a failing run it raises EvalGateFailure carrying
    the persisted EvalRun.
    """

    async def gate(
        prompt_name: str, version: PromptVersion, session: AsyncSession
    ) -> None:
        suite = await get_suite_for_prompt(prompt_name, session)
        if suite is None:
            return
        run = await run_eval_suite(
            suite,
            version,
            session,
            provider=provider,
            generate_model=generate_model,
            judge_model=judge_model,
            max_tokens=max_tokens,
        )
        if not run.passed:
            raise EvalGateFailure(run)

    return gate


async def run_suite_for_prompt(
    prompt_name: str,
    session: AsyncSession,
    *,
    provider,
    generate_model: str,
    judge_model: str,
    max_tokens: int,
    version_num: int | None = None,
    include_unreviewed: bool = False,
) -> EvalRun:
    """Resolve the suite and target version for `prompt_name`, then run the suite.

    Uses the active version unless `version_num` is given. Raises ValueError
    if no suite is registered, PromptNotFoundError if the prompt is unknown,
    and PromptVersionNotFoundError if `version_num` does not exist.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        raise ValueError(f"no eval suite registered for prompt {prompt_name!r}")

    if version_num is None:
        version = await get_active_prompt_version(prompt_name, session)
    else:
        prompt = await _get_prompt_row(prompt_name, session)
        from gatekeep.models import PromptVersion
        from gatekeep.prompts import PromptVersionNotFoundError

        version = (
            await session.execute(
                select(PromptVersion).where(
                    PromptVersion.prompt_id == prompt.id,
                    PromptVersion.version_num == version_num,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise PromptVersionNotFoundError(
                f"prompt {prompt_name!r} has no version {version_num}"
            )

    return await run_eval_suite(
        suite,
        version,
        session,
        provider=provider,
        generate_model=generate_model,
        judge_model=judge_model,
        max_tokens=max_tokens,
        include_unreviewed=include_unreviewed,
    )
