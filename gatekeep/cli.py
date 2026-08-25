from __future__ import annotations

import argparse
import asyncio
import json
import sys

from anthropic import AsyncAnthropic
from sqlalchemy import select

from gatekeep.accounts import account_service
from gatekeep.config import get_settings
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.prompts.curation import curate_cases, list_unreviewed, review_case
from gatekeep.prompts.evals import (
    EvalGateFailure,
    add_case,
    create_suite,
    get_suite_for_prompt,
    make_eval_gate,
    run_suite_for_prompt,
)
from gatekeep.prompts.fixtures import load_fixtures_dir
from gatekeep.prompts.prompts import (
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
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import Account, ApiKey, PromptVersion


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


async def _resolve_account_id(session, name: str) -> int:
    """Return the id of the account named `name`, or raise ValueError.

    Central lookup so every account/key subcommand references accounts by
    their human-facing name, matching how operators think about them.
    """
    account = (
        await session.execute(select(Account).where(Account.name == name))
    ).scalar_one_or_none()
    if account is None:
        raise ValueError(f"no account named {name!r}")
    return account.id


async def _account_create(name: str, budget: float | None, unlimited: bool, operator: bool) -> None:
    """Create an account, optionally with a budget cap and operator status.

    `--unlimited` and a positive `budget` are mutually exclusive ways to set
    the cap; omitting both leaves the account unlimited.
    """
    monthly = None if unlimited else budget
    async with SessionLocal() as session:
        account = await account_service.create_account(
            session, name=name, monthly_budget_usd=monthly, is_operator=operator
        )
    flag = " (operator)" if account.is_operator else ""
    print(f"created account {name!r}{flag}")


async def _account_rename(name: str, new_name: str) -> None:
    """Rename an account."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.rename_account(session, account_id, new_name)
    print(f"renamed {name!r} to {new_name!r}")


async def _account_set_budget(name: str, amount: float | None, unlimited: bool) -> None:
    """Set or clear an account's monthly USD spend cap, looked up by name."""
    if not unlimited and amount is None:
        raise ValueError("must provide an amount, or pass --unlimited to clear it")
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.set_budget(session, account_id, None if unlimited else amount)
    if unlimited:
        print(f"cleared budget cap for {name!r} (unlimited)")
    else:
        print(f"set budget cap for {name!r} to ${amount:.2f}/month")


async def _account_set_operator(name: str, off: bool) -> None:
    """Grant or revoke operator status for an account (guarded server-side)."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, name)
        await account_service.set_operator(session, account_id, not off)
    print(f"{'revoked' if off else 'granted'} operator for {name!r}")


async def _account_list() -> None:
    """Print every account with its budget and operator flag."""
    async with SessionLocal() as session:
        accounts = (await session.execute(select(Account).order_by(Account.name))).scalars().all()
        for account in accounts:
            budget = (
                "unlimited"
                if account.monthly_budget_usd is None
                else f"${account.monthly_budget_usd:.2f}"
            )
            flag = "\toperator" if account.is_operator else ""
            print(f"{account.name}\t{budget}{flag}")


async def _key_create(account: str, name: str) -> None:
    """Mint a key for an account and print the raw key exactly once."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        _key, raw = await account_service.create_key(session, account_id, name)
    print(raw)


async def _key_revoke(account: str, name: str) -> None:
    """Soft-revoke a key by account name and key name."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        key = (
            await session.execute(
                select(ApiKey).where(ApiKey.account_id == account_id, ApiKey.name == name)
            )
        ).scalar_one_or_none()
        if key is None:
            raise ValueError(f"no key named {name!r} on account {account!r}")
        await account_service.revoke_key(session, account_id, key.id)
    print(f"revoked key {name!r} on account {account!r}")


async def _key_list(account: str) -> None:
    """Print an account's keys (active and revoked)."""
    async with SessionLocal() as session:
        account_id = await _resolve_account_id(session, account)
        keys = await account_service.list_keys(session, account_id)
        for key in keys:
            status = "active" if key.active else "revoked"
            print(f"{key.name}\t{status}")


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

    account_parser = subparsers.add_parser("account", help="manage accounts (tenants)")
    account_subparsers = account_parser.add_subparsers(dest="account_command", required=True)

    ac_create = account_subparsers.add_parser("create", help="create an account")
    ac_create.add_argument("name")
    ac_create.add_argument("--budget", type=float, default=None, help="monthly cap in USD")
    ac_create.add_argument("--unlimited", action="store_true", help="no budget cap (default)")
    ac_create.add_argument("--operator", action="store_true", help="grant operator status")

    ac_rename = account_subparsers.add_parser("rename", help="rename an account")
    ac_rename.add_argument("name")
    ac_rename.add_argument("new_name")

    ac_budget = account_subparsers.add_parser(
        "set-budget", help="set or clear an account's monthly USD spend cap"
    )
    ac_budget.add_argument("name", help="the account name")
    ac_budget.add_argument(
        "amount", type=float, nargs="?", default=None, help="new monthly cap in USD"
    )
    ac_budget.add_argument(
        "--unlimited", action="store_true", help="clear the cap (unlimited spend)"
    )

    ac_operator = account_subparsers.add_parser(
        "set-operator", help="grant (default) or revoke operator status"
    )
    ac_operator.add_argument("name")
    ac_operator.add_argument(
        "--off", action="store_true", help="revoke operator status instead of granting"
    )

    account_subparsers.add_parser("list", help="list all accounts")

    key_parser = subparsers.add_parser("key", help="manage API keys")
    key_subparsers = key_parser.add_subparsers(dest="key_command", required=True)

    k_create = key_subparsers.add_parser("create", help="mint a key for an account")
    k_create.add_argument("account", help="the account name")
    k_create.add_argument("name", help="the new key's name")

    k_revoke = key_subparsers.add_parser("revoke", help="soft-revoke a key")
    k_revoke.add_argument("account", help="the account name")
    k_revoke.add_argument("name", help="the key's name")

    k_list = key_subparsers.add_parser("list", help="list an account's keys")
    k_list.add_argument("account", help="the account name")

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
        elif args.command == "account":
            if args.account_command == "create":
                asyncio.run(_account_create(args.name, args.budget, args.unlimited, args.operator))
            elif args.account_command == "rename":
                asyncio.run(_account_rename(args.name, args.new_name))
            elif args.account_command == "set-budget":
                asyncio.run(_account_set_budget(args.name, args.amount, args.unlimited))
            elif args.account_command == "set-operator":
                asyncio.run(_account_set_operator(args.name, args.off))
            elif args.account_command == "list":
                asyncio.run(_account_list())
        elif args.command == "key":
            if args.key_command == "create":
                asyncio.run(_key_create(args.account, args.name))
            elif args.key_command == "revoke":
                asyncio.run(_key_revoke(args.account, args.name))
            elif args.key_command == "list":
                asyncio.run(_key_list(args.account))
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
    except (
        PromptNotFoundError,
        PromptVersionNotFoundError,
        account_service.AccountServiceError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
