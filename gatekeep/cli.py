from __future__ import annotations

import argparse
import asyncio
import json
import sys

from anthropic import AsyncAnthropic

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
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    list_prompts,
    promote_prompt,
    rollback_prompt,
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


async def _list() -> None:
    """Print every registered prompt name with its current active version number."""
    async with SessionLocal() as session:
        prompts = await list_prompts(session)
        for prompt in prompts:
            version = await get_active_prompt_version(prompt.name, session)
            print(f"{prompt.name}\tv{version.version_num}")


async def _show(name: str) -> None:
    """Print the active version's number and template text for a prompt."""
    async with SessionLocal() as session:
        version = await get_active_prompt_version(name, session)
        print(f"# {name} (active version {version.version_num})")
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
        promoted = await promote_prompt(
            name, version_num, session, redis=redis, gate=gate
        )
    print(f"promoted {name!r} to version {promoted.version_num}")


async def _rollback(name: str) -> None:
    """Revert a prompt to its previous version, invalidating its cached responses."""
    redis = get_redis(get_settings())
    async with SessionLocal() as session:
        rolled_back = await rollback_prompt(name, session, redis=redis)
    print(f"rolled back {name!r} to version {rolled_back.version_num}")


async def _sync(directory: str) -> None:
    """Sync every prompts/*.txt file into the DB, adding versions where text changed."""
    import pathlib

    paths = sorted(pathlib.Path(directory).glob("*.txt"))
    async with SessionLocal() as session:
        for path in paths:
            template = path.read_text(encoding="utf-8")
            name = path.stem
            version = await sync_prompt_from_text(name, template, session)
            print(
                f"{name}\tv{version.version_num}{' (active)' if version.active else ' (new, not active)'}"
            )


async def _eval_create_suite(
    name: str, threshold: float | None, suite_name: str | None
) -> None:
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
    """Run a prompt's eval suite against a version/model, print the score, and return whether it passed.

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
    print(
        f"[{status}] {name!r} score={run.score:.2f} (run id {run.id}, model {run.model})"
    )
    return run.passed


async def _eval_load_fixtures(directory: str) -> None:
    """Load every prompts/*.cases.json fixture into the DB (idempotent)."""
    async with SessionLocal() as session:
        suites = await load_fixtures_dir(directory, session)
    for suite in suites:
        print(
            f"loaded fixture cases for {suite.prompt_name!r} (threshold {suite.pass_threshold})"
        )


async def _eval_curate(name: str, limit: int) -> None:
    """Mine recent request samples for a prompt into unreviewed curated cases."""
    async with SessionLocal() as session:
        cases = await curate_cases(name, session, limit=limit)
    print(
        f"curated {len(cases)} unreviewed case(s) for {name!r}; review them before they gate"
    )


async def _eval_review(name: str) -> None:
    """Interactively approve/reject each unreviewed curated case for a prompt."""
    async with SessionLocal() as session:
        pending = await list_unreviewed(name, session)
        if not pending:
            print(f"no unreviewed cases for {name!r}")
            return
        for case in pending:
            print(f"\ncase {case.id}: input={case.input_messages}")
            print(f"  judge_criteria: {case.judge_criteria}")
            answer = input("  approve? [y/N/q] ").strip().lower()
            if answer == "q":
                break
            await review_case(case.id, session, approve=(answer == "y"))
            print("  approved" if answer == "y" else "  rejected (deleted)")


def build_parser() -> argparse.ArgumentParser:
    """Build the `gatekeep` CLI's argument parser and its `prompt` subcommands."""
    parser = argparse.ArgumentParser(prog="gatekeep")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prompt_parser = subparsers.add_parser("prompt", help="manage prompt templates")
    prompt_subparsers = prompt_parser.add_subparsers(
        dest="prompt_command", required=True
    )

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

    show_parser = prompt_subparsers.add_parser(
        "show", help="show a prompt's active template"
    )
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

    sync_parser = prompt_subparsers.add_parser(
        "sync", help="sync all *.txt files from a directory into the DB"
    )
    sync_parser.add_argument("directory")

    eval_parser = subparsers.add_parser("eval", help="manage eval suites and cases")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)

    cs = eval_subparsers.add_parser(
        "create-suite", help="create an eval suite for a prompt"
    )
    cs.add_argument("name")
    cs.add_argument("--threshold", type=float, default=None)
    cs.add_argument("--name", dest="suite_name", default=None)

    ac = eval_subparsers.add_parser(
        "add-case", help="add a manual case from a JSON messages file"
    )
    ac.add_argument("name")
    ac.add_argument("--input-file", required=True)
    ac.add_argument(
        "--check-type", choices=["exact", "contains", "llm_judge"], required=True
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

    cu = eval_subparsers.add_parser(
        "curate", help="mine request samples into unreviewed cases"
    )
    cu.add_argument("name")
    cu.add_argument("--limit", type=int, default=10)

    rv = eval_subparsers.add_parser(
        "review", help="approve/reject unreviewed curated cases"
    )
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
            elif args.prompt_command == "sync":
                asyncio.run(_sync(args.directory))
        elif args.command == "eval":
            if args.eval_command == "create-suite":
                asyncio.run(
                    _eval_create_suite(args.name, args.threshold, args.suite_name)
                )
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
                    _eval_run(
                        args.name, args.version, args.model, args.include_unreviewed
                    )
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
