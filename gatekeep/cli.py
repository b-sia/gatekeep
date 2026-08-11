from __future__ import annotations

import argparse
import asyncio
import json
import sys

from anthropic import AsyncAnthropic
from sqlalchemy import select

from gatekeep.config import get_settings
from gatekeep.curation import curate_cases, list_unreviewed, review_case
from gatekeep.db import SessionLocal
from gatekeep.evals import (
    EvalGateFailure,
    add_case,
    create_suite,
    get_suite_for_prompt,
    make_eval_gate,
    run_suite_for_prompt,
)
from gatekeep.fixtures import load_fixtures_dir
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.models import ApiKey, PromptVersion
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    add_prompt_version,
    clear_candidate_version,
    create_prompt,
    get_active_prompt_version,
    get_prompt_row,
    list_prompts,
    promote_prompt,
    rollback_prompt,
    set_candidate_version,
    sync_prompt_from_text,
)
from gatekeep.providers.anthropic import AnthropicProvider


async def _create(name: str, template_file: str) -> None:
    """Create a new prompt from the template text in `template_file` (version 1, active)."""
    with open(template_file, encoding="utf-8") as f:
        template = f.read()
    async with SessionLocal() as session:
        await create_prompt(name, template, session)
    print(f"created prompt {name!r} at version 1")


async def _add_version(name: str, template_file: str) -> None:
    """Add a new, inactive version to an existing prompt (does not promote it)."""
    with open(template_file, encoding="utf-8") as f:
        template = f.read()
    async with SessionLocal() as session:
        version = await add_prompt_version(name, template, session)
    print(f"added version {version.version_num} to {name!r} (not active; promote it)")


async def _candidate_suffix(prompt, session) -> str:
    """Format a prompt's candidate state for CLI display, or "" if none is configured.

    A configured candidate is always shown, even at 0% traffic: a paused
    rollout (candidate kept configured, traffic pct dialed to 0) is a
    real, distinct state from no candidate being configured at all, even
    though both currently route 100% of requests to the active version.
    """
    if prompt.candidate_version_id is None:
        return ""
    candidate = await session.get(PromptVersion, prompt.candidate_version_id)
    pct = prompt.candidate_traffic_pct or 0.0
    paused = " (paused)" if pct == 0 else ""
    return f" (candidate: v{candidate.version_num} @ {pct}%{paused})"


async def _list() -> None:
    """Print every registered prompt name with its active version and candidate state."""
    async with SessionLocal() as session:
        prompts = await list_prompts(session)
        for prompt in prompts:
            version = await get_active_prompt_version(prompt.name, session)
            suffix = await _candidate_suffix(prompt, session)
            print(f"{prompt.name}\tv{version.version_num}{suffix}")


async def _show(name: str) -> None:
    """Print the active version's number/template and any configured candidate's state."""
    async with SessionLocal() as session:
        prompt = await get_prompt_row(name, session)
        version = await session.get(PromptVersion, prompt.active_version_id)
        suffix = await _candidate_suffix(prompt, session)
        print(f"# {name} (active version {version.version_num}){suffix}")
        print(version.template)


async def _promote(name: str, version_num: int) -> None:
    """Promote a prompt version to active, running its eval gate first if one exists."""
    settings = get_settings()
    redis = get_redis(settings)
    provider = AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key))
    gate = make_eval_gate(
        provider=provider,
        generate_model=settings.default_model,
        judge_model=settings.eval_judge_model,
        max_tokens=settings.default_max_tokens,
    )
    async with SessionLocal() as session:
        promoted = await promote_prompt(name, version_num, session, redis=redis, gate=gate)
    print(f"promoted {name!r} to version {promoted.version_num}")


async def _rollback(name: str) -> None:
    """Revert a prompt to its previous version, invalidating its cached responses."""
    redis = get_redis(get_settings())
    async with SessionLocal() as session:
        rolled_back = await rollback_prompt(name, session, redis=redis)
    print(f"rolled back {name!r} to version {rolled_back.version_num}")


async def _set_candidate(name: str, version_num: int, pct: float) -> None:
    """Configure an A/B candidate version + traffic percentage for a prompt.

    Lightweight compared to `promote`: does not run the eval gate and does
    not invalidate any cache, since the candidate isn't becoming "active".
    """
    async with SessionLocal() as session:
        prompt = await set_candidate_version(name, version_num, pct, session)
    print(
        f"set {name!r} candidate to version {version_num} "
        f"at {prompt.candidate_traffic_pct}% traffic"
    )


async def _clear_candidate(name: str) -> None:
    """Remove any configured A/B candidate for a prompt (100% back to active)."""
    async with SessionLocal() as session:
        await clear_candidate_version(name, session)
    print(f"cleared candidate for {name!r}; 100% of traffic now goes to the active version")


async def _sync(directory: str) -> None:
    """Sync every prompts/*.txt file into the DB, adding versions where text changed."""
    import pathlib

    paths = sorted(pathlib.Path(directory).glob("*.txt"))
    async with SessionLocal() as session:
        for path in paths:
            template = path.read_text(encoding="utf-8")
            name = path.stem
            version = await sync_prompt_from_text(name, template, session)
            status = "(active)" if version.active else "(new, not active)"
            print(f"{name}\tv{version.version_num}{status}")


async def _eval_create_suite(name: str, threshold: float | None, suite_name: str | None) -> None:
    """Create an eval suite for a prompt, defaulting the threshold from settings."""
    settings = get_settings()
    async with SessionLocal() as session:
        suite = await create_suite(
            name,
            session,
            pass_threshold=threshold
            if threshold is not None
            else settings.eval_pass_threshold_default,
            name=suite_name,
        )
    print(f"created eval suite for {name!r} (threshold {suite.pass_threshold})")


async def _eval_add_case(
    name: str,
    input_file: str,
    check_type: str,
    expected: str | None,
    judge_criteria: str | None,
) -> None:
    """Add a manual, reviewed case to a prompt's eval suite from a JSON messages file."""
    with open(input_file, encoding="utf-8") as f:
        input_messages = json.load(f)
    async with SessionLocal() as session:
        suite = await get_suite_for_prompt(name, session)
        if suite is None:
            raise ValueError(f"no eval suite registered for prompt {name!r}")
        case = await add_case(
            suite.id,
            session,
            input_messages=input_messages,
            check_type=check_type,
            expected=expected,
            judge_criteria=judge_criteria,
        )
    print(f"added {check_type} case {case.id} to {name!r}")


async def _eval_run(
    name: str, version: int | None, model: str | None, include_unreviewed: bool
) -> bool:
    """Run a prompt's eval suite against a version/model, print the score, and return the result.

    Returns:
        True if the eval suite passed, False otherwise.
    """
    settings = get_settings()
    provider = AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key))
    async with SessionLocal() as session:
        run = await run_suite_for_prompt(
            name,
            session,
            provider=provider,
            generate_model=model or settings.default_model,
            judge_model=settings.eval_judge_model,
            max_tokens=settings.default_max_tokens,
            version_num=version,
            include_unreviewed=include_unreviewed,
        )
    status = "PASS" if run.passed else "FAIL"
    print(f"[{status}] {name!r} score={run.score:.2f} (run id {run.id}, model {run.model})")
    return run.passed


async def _eval_load_fixtures(directory: str) -> None:
    """Load every prompts/*.cases.json fixture into the DB (idempotent)."""
    async with SessionLocal() as session:
        suites = await load_fixtures_dir(directory, session)
    for suite in suites:
        print(f"loaded fixture cases for {suite.prompt_name!r} (threshold {suite.pass_threshold})")


async def _eval_curate(name: str, limit: int) -> None:
    """Mine recent request samples for a prompt into unreviewed curated cases.

    Each case's judge_criteria is auto-generated from its sampled input/output
    (see `evals.generate_judge_criteria`) using the gateway's default provider
    and model, so `gatekeep eval review` has a proposed rubric to show.
    """
    settings = get_settings()
    provider = AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key))
    async with SessionLocal() as session:
        cases = await curate_cases(
            name,
            session,
            limit=limit,
            provider=provider,
            generate_model=settings.default_model,
        )
    print(f"curated {len(cases)} unreviewed case(s) for {name!r}; review them before they gate")


async def _eval_review(name: str) -> None:
    """Interactively approve/reject each unreviewed curated case for a prompt.

    Curated cases arrive with an auto-generated `judge_criteria` proposal
    (see `curation.curate_cases`); `e` lets the reviewer edit it in place
    before approving, so the human's job is tightening a draft rather than
    writing a rubric from scratch.
    """
    async with SessionLocal() as session:
        pending = await list_unreviewed(name, session)
        if not pending:
            print(f"no unreviewed cases for {name!r}")
            return
        for case in pending:
            print(f"\ncase {case.id}: input={case.input_messages}")
            print(f"  judge_criteria: {case.judge_criteria}")
            answer = input("  approve? [y/N/e=edit/q] ").strip().lower()
            if answer == "q":
                break
            if answer == "e":
                new_criteria = input("  new judge_criteria: ").strip()
                if new_criteria:
                    case.judge_criteria = new_criteria
                    await session.commit()
                answer = input("  approve? [y/N/q] ").strip().lower()
                if answer == "q":
                    break
            await review_case(case.id, session, approve=(answer == "y"))
            print("  approved" if answer == "y" else "  rejected (deleted)")


async def _set_budget(name: str, amount: float | None, unlimited: bool) -> None:
    """Set or clear an API key's monthly USD spend cap, looked up by name.

    Args:
        name: The api_keys.name of the key to update.
        amount: The new monthly_budget_usd value, or None if `unlimited` is set.
        unlimited: If True, clears the cap (monthly_budget_usd = None),
            ignoring `amount`.

    Raises:
        ValueError: if neither `amount` nor `unlimited` was given, if
            `amount` is not positive, or if no key with that name exists.
    """
    if not unlimited and amount is None:
        raise ValueError("must provide an amount, or pass --unlimited to clear it")
    if not unlimited and amount <= 0:
        raise ValueError("amount must be positive")
    async with SessionLocal() as session:
        key = (
            await session.execute(select(ApiKey).where(ApiKey.name == name))
        ).scalar_one_or_none()
        if key is None:
            raise ValueError(f"no API key named {name!r}")
        key.monthly_budget_usd = None if unlimited else amount
        await session.commit()
    if unlimited:
        print(f"cleared budget cap for {name!r} (unlimited)")
    else:
        print(f"set budget cap for {name!r} to ${amount:.2f}/month")


def build_parser() -> argparse.ArgumentParser:
    """Build the `gatekeep` CLI's argument parser and its `prompt` subcommands."""
    parser = argparse.ArgumentParser(prog="gatekeep")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt_parser = subparsers.add_parser("prompt", help="manage prompt templates")
    prompt_subparsers = prompt_parser.add_subparsers(dest="prompt_command", required=True)

    create_parser = prompt_subparsers.add_parser(
        "create", help="create a new prompt from a template file"
    )
    create_parser.add_argument("name")
    create_parser.add_argument("template_file")

    add_version_parser = prompt_subparsers.add_parser(
        "add-version",
        help="add a new (inactive) version to an existing prompt; promote it to activate",
    )
    add_version_parser.add_argument("name")
    add_version_parser.add_argument("template_file")

    prompt_subparsers.add_parser("list", help="list all prompts and active versions")

    show_parser = prompt_subparsers.add_parser("show", help="show a prompt's active template")
    show_parser.add_argument("name")

    promote_parser = prompt_subparsers.add_parser(
        "promote", help="promote an existing prompt version to active"
    )
    promote_parser.add_argument("name")
    promote_parser.add_argument("version", type=int)

    rollback_parser = prompt_subparsers.add_parser(
        "rollback", help="revert a prompt to its previous version"
    )
    rollback_parser.add_argument("name")

    set_candidate_parser = prompt_subparsers.add_parser(
        "set-candidate",
        help="route a percentage of traffic to a candidate version (A/B test)",
    )
    set_candidate_parser.add_argument("name")
    set_candidate_parser.add_argument("version", type=int)
    set_candidate_parser.add_argument(
        "--pct",
        type=float,
        required=True,
        help="percentage (0-100) of traffic to route to the candidate version",
    )

    clear_candidate_parser = prompt_subparsers.add_parser(
        "clear-candidate", help="remove a prompt's configured A/B candidate"
    )
    clear_candidate_parser.add_argument("name")

    sync_parser = prompt_subparsers.add_parser(
        "sync", help="sync all *.txt files from a directory into the DB"
    )
    sync_parser.add_argument("directory")

    key_parser = subparsers.add_parser("key", help="manage API keys")
    key_subparsers = key_parser.add_subparsers(dest="key_command", required=True)

    set_budget_parser = key_subparsers.add_parser(
        "set-budget", help="set or clear a key's monthly USD spend cap"
    )
    set_budget_parser.add_argument("name")
    set_budget_parser.add_argument(
        "amount", type=float, nargs="?", default=None, help="new monthly cap in USD"
    )
    set_budget_parser.add_argument(
        "--unlimited", action="store_true", help="clear the cap (unlimited spend)"
    )

    eval_parser = subparsers.add_parser("eval", help="manage eval suites and cases")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)

    cs = eval_subparsers.add_parser("create-suite", help="create an eval suite for a prompt")
    cs.add_argument("name")
    cs.add_argument("--threshold", type=float, default=None)
    cs.add_argument("--name", dest="suite_name", default=None)

    ac = eval_subparsers.add_parser("add-case", help="add a manual case from a JSON messages file")
    ac.add_argument("name")
    ac.add_argument("--input-file", required=True)
    ac.add_argument(
        "--check-type",
        choices=["exact", "contains", "icontains", "llm_judge"],
        required=True,
    )
    ac.add_argument("--expected", default=None)
    ac.add_argument("--judge-criteria", default=None)

    er = eval_subparsers.add_parser("run", help="run a prompt's eval suite")
    er.add_argument("name")
    er.add_argument("--version", type=int, default=None)
    er.add_argument("--model", default=None)
    er.add_argument("--include-unreviewed", action="store_true")

    lf = eval_subparsers.add_parser(
        "load-fixtures", help="load prompts/*.cases.json fixtures into the DB"
    )
    lf.add_argument("directory")

    cu = eval_subparsers.add_parser("curate", help="mine request samples into unreviewed cases")
    cu.add_argument("name")
    cu.add_argument("--limit", type=int, default=10)

    rv = eval_subparsers.add_parser("review", help="approve/reject unreviewed curated cases")
    rv.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `gatekeep` console script; dispatches `prompt` subcommands."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "prompt":
            if args.prompt_command == "create":
                asyncio.run(_create(args.name, args.template_file))
            elif args.prompt_command == "add-version":
                asyncio.run(_add_version(args.name, args.template_file))
            elif args.prompt_command == "list":
                asyncio.run(_list())
            elif args.prompt_command == "show":
                asyncio.run(_show(args.name))
            elif args.prompt_command == "promote":
                asyncio.run(_promote(args.name, args.version))
            elif args.prompt_command == "rollback":
                asyncio.run(_rollback(args.name))
            elif args.prompt_command == "set-candidate":
                asyncio.run(_set_candidate(args.name, args.version, args.pct))
            elif args.prompt_command == "clear-candidate":
                asyncio.run(_clear_candidate(args.name))
            elif args.prompt_command == "sync":
                asyncio.run(_sync(args.directory))
        elif args.command == "key":
            if args.key_command == "set-budget":
                asyncio.run(_set_budget(args.name, args.amount, args.unlimited))
        elif args.command == "eval":
            if args.eval_command == "create-suite":
                asyncio.run(_eval_create_suite(args.name, args.threshold, args.suite_name))
            elif args.eval_command == "add-case":
                asyncio.run(
                    _eval_add_case(
                        args.name,
                        args.input_file,
                        args.check_type,
                        args.expected,
                        args.judge_criteria,
                    )
                )
            elif args.eval_command == "run":
                passed = asyncio.run(
                    _eval_run(args.name, args.version, args.model, args.include_unreviewed)
                )
                if not passed:
                    return 2
            elif args.eval_command == "load-fixtures":
                asyncio.run(_eval_load_fixtures(args.directory))
            elif args.eval_command == "curate":
                asyncio.run(_eval_curate(args.name, args.limit))
            elif args.eval_command == "review":
                asyncio.run(_eval_review(args.name))
    except EvalGateFailure as exc:
        run = exc.eval_run
        print(f"error: {exc}", file=sys.stderr)
        for item in run.report:
            status = "PASS" if item["passed"] else "FAIL"
            print(
                f"  [{status}] case {item['case_id']}: {item['reason']}",
                file=sys.stderr,
            )
        return 2
    except (PromptNotFoundError, PromptVersionNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
