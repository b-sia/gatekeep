"""Populate a fresh development database with realistic records.

Running the stack against an empty database leaves every dashboard panel,
the account-management tab, and the prompts tab blank, so local work starts
with a tedious round of hand-creating accounts, keys, prompts, and traffic.
This script does all of that in one shot, driving the same service layer the
product uses (so the data is shaped exactly like real data) and filling in
`request_logs` history directly for the analytics dashboard.

What it creates:

  * Accounts + logins (account-management tab): one operator account and
    three tenant accounts, each with an email/password dashboard login using
    a shared dev password, plus one `pending` account to fill the operator
    approval queue. Every active account gets one or two API keys, printed
    once at the end.
  * Prompts (prompts tab): a few named prompts with multiple versions
    (one active, one wired up as an A/B candidate), eval suites and cases
    loaded from the `prompts/` fixtures, a short eval-run history per prompt
    (an early failing run and a passing one, where there's more than one
    version), a corpus of request samples, and curated cases mined from
    them - one reviewed, the rest left pending in the curation queue.
  * Audit log: an `audit_events` row for every mutation above (account
    creation, key minting, prompt/version/promotion, candidate rollout,
    eval-suite/case/run, curation mining and review, budget updates), using
    the same action names the real dashboard endpoints record.
  * Request history (dashboard tab): ~30 days of `request_logs` across the
    tenant accounts and keys - several models across providers, a realistic
    mix of cached and provider-served requests, varied latency and outcomes,
    and costs computed from the real pricing table.

Idempotency: without `--reset` the script get-or-creates accounts and prompts
and only adds request history when an account has none, so a second run on a
populated database is a near no-op rather than a crash. Pass `--reset` to
TRUNCATE the seed-owned tables and rebuild everything from scratch - the
normal path after nuking the dev database.

Usage:
    python scripts/seed_dev.py [--reset] [--password PASSWORD]
    just seed          # idempotent top-up
    just seed-reset    # wipe seed tables, then repopulate
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounts import account_service, auth_service
from gatekeep.audit.audit import record_audit_event
from gatekeep.caching.redis_token_bucket import get_redis
from gatekeep.config import get_settings
from gatekeep.prompts import prompts as prompt_service
from gatekeep.prompts.curation import CURATED_JUDGE_CRITERIA
from gatekeep.prompts.evals import add_case, create_suite, get_suite_for_prompt
from gatekeep.prompts.fixtures import load_fixtures_dir
from gatekeep.prompts.samples import record_request_sample
from gatekeep.routing.pricing import get_pricing_table
from gatekeep.storage.db import SessionLocal
from gatekeep.storage.models import (
    Account,
    ApiKey,
    EvalCase,
    EvalRun,
    Prompt,
    PromptVersion,
    RequestLog,
)

# Deterministic RNG so re-runs (and reviews of the generated data) are stable.
_RNG = random.Random(20260828)

DEFAULT_PASSWORD = "password123"

# Tables the seeder owns end-to-end. `--reset` truncates these with CASCADE
# and RESTART IDENTITY so a repopulate starts from a clean slate with ids
# counting from 1 again. Ordered child-before-parent for readability; CASCADE
# makes the exact order immaterial.
_SEED_TABLES = (
    "audit_events",
    "request_logs",
    "request_samples",
    "cached_responses",
    "eval_runs",
    "eval_cases",
    "eval_suites",
    "prompt_versions",
    "prompts",
    "sessions",
    "email_tokens",
    "account_credentials",
    "api_keys",
    "accounts",
)

# Hostnames `--reset` is allowed to TRUNCATE against: the host-mapped port
# this script normally runs through, and the docker-compose service name
# (docker-compose.yml), for completeness if this is ever run from inside the
# network. Deliberately not configurable from the CLI - a real need to reset
# a differently-hosted *disposable* database is rare enough to just edit this
# set, and that friction is the point.
_SAFE_RESET_HOSTS = {"localhost", "127.0.0.1", "postgres"}


def _assert_reset_target_is_safe(database_url: str) -> None:
    """Refuse to proceed if `database_url` doesn't look like a local dev database.

    `--reset` issues a `TRUNCATE ... CASCADE` across every seed-owned table,
    including `audit_events` - irreversible, and with no confirmation from
    the database's own history once it's done. The only realistic way this
    runs against the wrong database is a misconfigured `DATABASE_URL` (a
    stale `.env`, a copy-pasted export, the wrong shell profile), so this
    checks the hostname before touching anything.

    Raises:
        SystemExit: if the URL's host isn't in `_SAFE_RESET_HOSTS`.
    """
    host = (make_url(database_url).host or "").lower()
    if host not in _SAFE_RESET_HOSTS:
        raise SystemExit(
            f"Refusing --reset: DATABASE_URL host {host!r} is not a recognized local "
            f"dev host ({', '.join(sorted(_SAFE_RESET_HOSTS))}).\n"
            "This flag TRUNCATEs every seed-owned table, including the audit log. "
            "If this really is a disposable local database, add its host to "
            "_SAFE_RESET_HOSTS in scripts/seed_dev.py and re-run."
        )


def _confirm_reset(database_url: str) -> None:
    """Prompt for the target database's name before truncating it.

    A second, cheap guard on top of `_assert_reset_target_is_safe`: even a
    `localhost` database can be someone's real work (a demo they're mid-way
    through building, a bug they're reproducing). Typing the exact database
    name back is enough friction to stop a reflexive `--reset` on the wrong
    terminal tab without being annoying for the common case.

    Raises:
        SystemExit: if the typed input doesn't match the database name.
    """
    url = make_url(database_url)
    target = f"{url.host}:{url.port}/{url.database}"
    reply = input(
        f"About to TRUNCATE seed-owned tables on '{target}'.\n"
        f"Type the database name ({url.database!r}) to continue: "
    )
    if reply != url.database:
        raise SystemExit("Aborted: confirmation did not match. No changes made.")


# Concrete (provider, model) pairs that exist in the vendored pricing table,
# so cost is computed from real per-token rates rather than guessed. Ollama is
# intentionally absent from pricing (local models are free); an ollama row
# therefore lands at cost 0, which is the real product behavior.
_MODEL_POOL: list[tuple[str, str]] = [
    ("anthropic", "claude-4-sonnet-20250514"),
    ("anthropic", "claude-haiku-4-5"),
    ("anthropic", "claude-3-opus-20240229"),
    ("openai", "gpt-4o"),
    ("openai", "gpt-4o-mini"),
    ("google", "gemini-2.5-flash"),
    ("google", "gemini-2.0-flash"),
    ("ollama", "llama3"),
]
# Selection weights favor mid-tier workhorse models over the cheapest/free
# ones, so generated spend sits in a believable range rather than being
# dragged toward zero by an even split across free and budget models.
_MODEL_WEIGHTS = [0.28, 0.12, 0.10, 0.18, 0.08, 0.10, 0.06, 0.08]

# Served-request paths and their share of traffic. Cache hits (exact/semantic)
# make up roughly a third of requests, matching a warmed cache.
_PATHS = ["provider", "stream", "cache_exact", "cache_semantic"]
_PATH_WEIGHTS = [0.42, 0.24, 0.20, 0.14]
_CACHED_PATHS = {"cache_exact", "cache_semantic"}

# Outcomes: mostly successful, with a small tail of provider errors and client
# disconnects so the dashboard success-rate figure is not a flat 100%.
_OUTCOMES = ["ok", "provider_error", "client_disconnect"]
_OUTCOME_WEIGHTS = [0.94, 0.04, 0.02]


@dataclass
class SeededAccount:
    """An account created by the seeder, with the artifacts callers need later.

    Attributes:
        account: The persisted Account row.
        email: Dashboard login email, or None for logins-less accounts.
        keys: List of (key_name, raw_key) minted on the account. Raw keys are
            only knowable at creation time, so they are captured here to print
            once at the end.
        budget_ratio: Target fraction of the monthly budget this account's
            current-month billed spend should represent. The budget is derived
            from the spend actually generated (spend / ratio, rounded up), so
            the budget-card fill lands near this fraction regardless of the
            absolute dollar amounts. None means the account keeps whatever
            budget it was created with (a fixed cap, or unlimited).
    """

    account: Account
    email: str | None
    keys: list[tuple[str, str]] = field(default_factory=list)
    budget_ratio: float | None = None


async def reset_seed_tables(session: AsyncSession) -> None:
    """TRUNCATE every seed-owned table, resetting identities.

    One statement so the truncation is atomic and CASCADE handles foreign
    keys regardless of table order. Only ever run under `--reset`.
    """
    joined = ", ".join(_SEED_TABLES)
    await session.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))
    await session.commit()


async def _get_or_create_account(
    session: AsyncSession,
    *,
    name: str,
    monthly_budget_usd: float | None,
    is_operator: bool,
    status: str,
) -> tuple[Account, bool]:
    """Return the account named `name`, creating it with these attributes if absent.

    Returns:
        A (account, created) pair - `created` is False when the account
        already existed, so callers can gate audit-event emission on it.
    """
    existing = (
        await session.execute(select(Account).where(Account.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    account = await account_service.create_account(
        session,
        name=name,
        monthly_budget_usd=monthly_budget_usd,
        is_operator=is_operator,
        status=status,
    )
    return account, True


async def _ensure_login(session: AsyncSession, account: Account, email: str, password: str) -> None:
    """Give `account` a verified email/password login, unless it already has one."""
    try:
        await auth_service.set_initial_credentials(
            session, account_id=account.id, email=email, password=password
        )
    except auth_service.CredentialsAlreadySetError:
        pass


async def _ensure_key(session: AsyncSession, account: Account, key_name: str) -> str | None:
    """Mint `key_name` on `account`, returning the raw key, or None if it already exists."""
    existing = (
        await session.execute(
            select(ApiKey).where(ApiKey.account_id == account.id, ApiKey.name == key_name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    _key, raw = await account_service.create_key(session, account.id, key_name)
    return raw


async def seed_accounts(session: AsyncSession, password: str) -> list[SeededAccount]:
    """Create the operator, tenant, and pending accounts with logins and keys.

    Args:
        session: Active async DB session.
        password: Shared dashboard-login password for every account that has a
            login.

    Returns:
        The seeded accounts, in creation order, each carrying its raw keys and
        an optional budget-fill ratio used to derive its cap from real spend.
    """
    # (name, initial_budget, operator, status, email, [key names], budget_ratio)
    #
    # `initial_budget` is set only where it is a deliberate fixed cap (the
    # low-traffic operator account) or unlimited (None). For the tenants whose
    # budget-card fill we want to control, the cap is left None here and set
    # later from the spend actually generated, per `budget_ratio`.
    specs: list[tuple[str, float | None, bool, str, str | None, list[str], float | None]] = [
        ("acme-corp", 500.0, True, "approved", "operator@gatekeep.dev", ["acme-prod"], None),
        (
            "globex",
            None,
            False,
            "approved",
            "globex@gatekeep.dev",
            ["globex-prod", "globex-staging"],
            0.35,
        ),
        ("initech", None, False, "approved", "initech@gatekeep.dev", ["initech-prod"], 0.9),
        # Unlimited budget: no ratio, no cap.
        (
            "hooli",
            None,
            False,
            "approved",
            "hooli@gatekeep.dev",
            ["hooli-prod", "hooli-batch"],
            None,
        ),
        # No login and no keys: this one only exists to populate the operator's
        # pending-approval queue on the accounts tab.
        ("pending-startup", None, False, "pending", None, [], None),
    ]

    seeded: list[SeededAccount] = []
    operator: Account | None = None
    for name, budget, is_operator, status, email, key_names, ratio in specs:
        account, created = await _get_or_create_account(
            session,
            name=name,
            monthly_budget_usd=budget,
            is_operator=is_operator,
            status=status,
        )
        if created:
            # The operator account has no operator yet to act as its own
            # audit actor - it is the out-of-band bootstrap, same as
            # scripts/create_key.py's --operator path. Every other account is
            # created "by" that operator, matching the real account.create flow.
            await record_audit_event(
                session,
                actor_account_id=operator.id if operator else None,
                actor_label=operator.name if operator else "seed script (bootstrap)",
                action="account.create",
                entity_type="account",
                entity_ref=account.name,
                result="success",
                details={"is_operator": is_operator, "status": status},
            )
        if is_operator:
            operator = account
        if email is not None:
            await _ensure_login(session, account, email, password)
        keys: list[tuple[str, str]] = []
        for key_name in key_names:
            raw = await _ensure_key(session, account, key_name)
            if raw is not None:
                keys.append((key_name, raw))
                await record_audit_event(
                    session,
                    actor_account_id=account.id,
                    actor_label=account.name,
                    action="key.mint",
                    entity_type="api_key",
                    entity_ref=key_name,
                    result="success",
                    details={},
                )
        seeded.append(SeededAccount(account=account, email=email, keys=keys, budget_ratio=ratio))
    return seeded


# Prompt templates: each entry is a list of successive version templates. The
# last is promoted to active; where a candidate is configured below it points
# at an earlier version to model an in-flight A/B rollout.
_PROMPT_SPECS: dict[str, list[str]] = {
    "support-triage": [
        "You are a support triage assistant. Classify the ticket as one of: "
        "billing, technical, account. Reply with only the label.",
        "You are a support triage assistant. Classify the customer ticket into "
        "exactly one category: billing, technical, or account. Respond with the "
        "single lowercase label and nothing else.",
        "You triage incoming support tickets. Read the message and return one "
        "category label from {billing, technical, account}. Output only the label, "
        "lowercase, no punctuation.",
    ],
    "summarizer": [
        "Summarize the following text in one sentence.",
        "Summarize the following text in a single clear sentence of at most 30 "
        "words. Preserve the key fact and drop filler.",
    ],
    # Single version; its eval suite/cases ship as prompts/system-context.cases.json
    # and are loaded by the fixtures loader, so the prompt appears on the tab
    # already wired to an eval suite.
    "system-context": [
        "You are a helpful assistant. Answer clearly and concisely.",
    ],
}

# Eval-suite thresholds for prompts the seeder registers a suite for directly
# (the fixtures loader handles suites that ship as *.cases.json files).
_SUITE_THRESHOLD = 0.8

_SAMPLE_INPUTS: dict[str, list[tuple[str, str]]] = {
    "support-triage": [
        ("My invoice charged me twice this month.", "billing"),
        ("The API returns a 500 whenever I stream responses.", "technical"),
        ("I need to reset the password on my login.", "account"),
        ("Can you explain the line items on my latest receipt?", "billing"),
    ],
    "summarizer": [
        (
            "The quarterly report shows revenue up 12% driven by enterprise "
            "renewals, though churn ticked up in the SMB segment.",
            "Revenue rose 12% on enterprise renewals despite higher SMB churn.",
        ),
        (
            "After the migration the p99 latency dropped from 800ms to 180ms "
            "and error rates fell below 0.1%.",
            "The migration cut p99 latency to 180ms and errors below 0.1%.",
        ),
    ],
}


async def seed_prompts(
    session: AsyncSession, tenants: list[SeededAccount], *, operator: Account
) -> None:
    """Create prompts with version history, A/B candidates, evals, and samples.

    Loads the `prompts/` fixture suites, builds the multi-version prompts in
    `_PROMPT_SPECS`, wires an A/B candidate onto `support-triage`, records a
    short eval-run history and curated (mined) cases per prompt, and writes a
    handful of request samples for curation. Every mutation is paired with an
    `audit_events` row using the same action names the real dashboard
    endpoints record, so the audit feed shows realistic operator activity.

    Args:
        session: Active async DB session.
        tenants: Seeded tenant accounts, used to attribute request samples and
            curated cases to a real account id.
        operator: The operator account, recorded as the actor on every
            prompt/eval/curation audit event (matches the real dashboard,
            where these routes are operator-only).
    """
    # Fixture-backed suites (e.g. system-context) come with cases attached.
    await load_fixtures_dir("prompts", session)

    for name, templates in _PROMPT_SPECS.items():
        existing = (
            await session.execute(select(Prompt).where(Prompt.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            continue

        await prompt_service.create_prompt(
            name, templates[0], session, created_by="seed", notes="initial version"
        )
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="prompt.create",
            entity_type="prompt",
            entity_ref=name,
            version_num=1,
            result="success",
            details={"notes": "initial version"},
        )
        for idx, template in enumerate(templates[1:], start=2):
            await prompt_service.add_prompt_version(
                name, template, session, created_by="seed", notes=f"iteration {idx}"
            )
            await record_audit_event(
                session,
                actor_account_id=operator.id,
                actor_label=operator.name,
                action="prompt.add_version",
                entity_type="prompt",
                entity_ref=name,
                version_num=idx,
                result="success",
                details={"notes": f"iteration {idx}"},
            )
        # Promote the latest version to active (ungated: the eval gate would
        # call a live provider, which a seed must never depend on).
        await prompt_service.promote_prompt(name, len(templates), session)
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="prompt.promote",
            entity_type="prompt",
            entity_ref=name,
            version_num=len(templates),
            result="success",
            details={},
        )

    # Model an in-flight A/B rollout: send 20% of support-triage traffic to the
    # previous version as a candidate.
    triage = (
        await session.execute(select(Prompt).where(Prompt.name == "support-triage"))
    ).scalar_one_or_none()
    if triage is not None and triage.candidate_version_id is None:
        await prompt_service.set_candidate_version("support-triage", 2, 20.0, session)
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="prompt.set_candidate",
            entity_type="prompt",
            entity_ref="support-triage",
            version_num=2,
            result="success",
            details={"traffic_pct": 20.0},
        )

    await _seed_eval_cases_and_runs(session, operator=operator)
    sample_owners = await _seed_request_samples(session, tenants)
    await _seed_curation(session, sample_owners, operator=operator)


async def _seed_eval_cases_and_runs(session: AsyncSession, *, operator: Account) -> None:
    """Register eval suites/cases for the `_PROMPT_SPECS` prompts and a short run history.

    Runs are inserted directly as EvalRun rows rather than executed, since
    scoring cases would require a live provider. For a prompt with more than
    one version this seeds two runs - an earlier, failing run against the
    first version and a passing run against the active one - so the
    prompt-detail eval-history chart shows a real improvement trend rather
    than a single flat point. A single-version prompt (system-context) gets
    one passing run. Each newly created suite/case/run is paired with an
    `eval.create_suite` / `eval.add_case` / `eval.run` audit event.
    """
    for name, samples in _SAMPLE_INPUTS.items():
        suite = await get_suite_for_prompt(name, session)
        if suite is None:
            suite = await create_suite(name, session, pass_threshold=_SUITE_THRESHOLD)
            await record_audit_event(
                session,
                actor_account_id=operator.id,
                actor_label=operator.name,
                action="eval.create_suite",
                entity_type="eval_suite",
                entity_ref=name,
                result="success",
                details={"pass_threshold": _SUITE_THRESHOLD},
            )
            for prompt_text, expected in samples:
                await add_case(
                    suite.id,
                    session,
                    input_messages=[{"role": "user", "content": prompt_text}],
                    check_type="icontains",
                    expected=expected,
                    reviewed=True,
                    source="fixture",
                )
                await record_audit_event(
                    session,
                    actor_account_id=operator.id,
                    actor_label=operator.name,
                    action="eval.add_case",
                    entity_type="eval_suite",
                    entity_ref=name,
                    result="success",
                    details={"check_type": "icontains"},
                )

        versions = list(
            (
                await session.execute(
                    select(PromptVersion)
                    .join(Prompt, Prompt.id == PromptVersion.prompt_id)
                    .where(Prompt.name == name)
                    .order_by(PromptVersion.version_num)
                )
            )
            .scalars()
            .all()
        )
        if not versions:
            continue
        has_run = (
            await session.execute(select(EvalRun.id).where(EvalRun.suite_id == suite.id).limit(1))
        ).first()
        if has_run is not None:
            continue

        report = [
            {
                "input": [{"role": "user", "content": prompt_text}],
                "expected": expected,
                "check_type": "icontains",
                "passed": True,
                "output": expected,
            }
            for prompt_text, expected in samples
        ]
        runs_to_add = [(versions[-1], 1.0, True, report)]
        if len(versions) > 1:
            # An earlier, weaker run: half the cases pass, scoring below the
            # 0.8 threshold, so the eval-history trend shows real improvement.
            failing_report = [{**r, "passed": i == 0} for i, r in enumerate(report)]
            failing_score = sum(1 for r in failing_report if r["passed"]) / len(failing_report)
            runs_to_add.insert(0, (versions[0], failing_score, False, failing_report))

        for version, score, passed, run_report in runs_to_add:
            session.add(
                EvalRun(
                    suite_id=suite.id,
                    prompt_version_id=version.id,
                    model="claude-4-sonnet-20250514",
                    score=score,
                    passed=passed,
                    report=run_report,
                )
            )
            await session.commit()
            await record_audit_event(
                session,
                actor_account_id=operator.id,
                actor_label=operator.name,
                action="eval.run",
                entity_type="eval_suite",
                entity_ref=name,
                version_num=version.version_num,
                result="success" if passed else "blocked",
                details={"score": score, "model": "claude-4-sonnet-20250514"},
            )


async def _seed_request_samples(
    session: AsyncSession, tenants: list[SeededAccount]
) -> dict[str, SeededAccount]:
    """Write a few request samples per prompt so curation has a corpus to mine.

    Returns:
        A mapping of prompt name to the tenant account its samples (and any
        curated cases derived from them) were attributed to, so
        `_seed_curation` stays consistent with the underlying traffic.
    """
    key_owners = [t for t in tenants if t.keys]
    if not key_owners:
        return {}

    owners: dict[str, SeededAccount] = {}
    for name, samples in _SAMPLE_INPUTS.items():
        owner = _RNG.choice(key_owners)
        owners[name] = owner
        key = (
            await session.execute(
                select(ApiKey).where(ApiKey.account_id == owner.account.id).limit(1)
            )
        ).scalar_one_or_none()
        if key is None:
            continue
        # Only add samples if this prompt has none yet (keeps re-runs quiet).
        for prompt_text, expected in samples:
            await record_request_sample(
                session,
                key_id=key.id,
                account_id=owner.account.id,
                prompt_name=name,
                model="claude-4-sonnet-20250514",
                input_messages=[{"role": "user", "content": prompt_text}],
                output_text=expected,
            )
    return owners


async def _seed_curation(
    session: AsyncSession, sample_owners: dict[str, SeededAccount], *, operator: Account
) -> None:
    """Mine each prompt's samples into curated eval cases, review one, leave the rest pending.

    Inserts `EvalCase` rows directly with `source="curated"` rather than
    calling `curation.curate_cases`, since real curation generates each
    case's `judge_criteria` via a live LLM call - a seed must never depend
    on one. Uses the same generic rubric `curate_cases` falls back to on a
    generation failure (`CURATED_JUDGE_CRITERIA`), so the cases are shaped
    exactly like the real fallback path. The first sample's case is approved
    (`reviewed=True`) to show that state; the rest are left unreviewed so the
    dashboard's curation-review queue has something in it. Paired with
    `curation.mine` and `curation.review` audit events, matching the real
    dashboard routes. Skips a prompt whose suite already has curated cases,
    so a re-run stays quiet.
    """
    for name, samples in _SAMPLE_INPUTS.items():
        owner = sample_owners.get(name)
        if owner is None:
            continue
        suite = await get_suite_for_prompt(name, session)
        if suite is None:
            continue
        already_curated = (
            await session.execute(
                select(EvalCase.id)
                .where(EvalCase.suite_id == suite.id, EvalCase.source == "curated")
                .limit(1)
            )
        ).first()
        if already_curated is not None:
            continue

        cases: list[EvalCase] = []
        for prompt_text, _expected in samples:
            case = await add_case(
                suite.id,
                session,
                input_messages=[{"role": "user", "content": prompt_text}],
                check_type="llm_judge",
                judge_criteria=CURATED_JUDGE_CRITERIA,
                reviewed=False,
                source="curated",
                account_id=owner.account.id,
            )
            cases.append(case)
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="curation.mine",
            entity_type="prompt",
            entity_ref=name,
            result="success",
            details={"count": len(cases)},
        )

        # Approve the first mined case so the curation tab shows both states.
        first = cases[0]
        first.reviewed = True
        await session.commit()
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="curation.review",
            entity_type="curated_case",
            entity_ref=str(first.id),
            result="success",
            details={"approved": True},
        )


# Prompt names (and their active version number) that some request rows are
# tagged with, so the dashboard's per-prompt breakdown and the prompt-scoped
# columns on request_logs are populated. Kept in sync with `_PROMPT_SPECS`.
_PROMPT_TAGS: list[tuple[str, int]] = [
    ("support-triage", 3),
    ("summarizer", 2),
    ("system-context", 1),
]


def _make_request_log(*, account_id: int, key_id: int, created_at: datetime) -> RequestLog:
    """Build one plausible RequestLog row with cost computed from the pricing table.

    Token counts are drawn from a production-scale range so per-request cost
    lands in the cents-to-dimes band real traffic produces. Path (and thus
    cache status), latency, outcome, and an optional prompt tag are all sampled
    consistently with one another.

    Args:
        account_id: Owning account.
        key_id: Key the request was made with.
        created_at: Timestamp for the row.

    Returns:
        An unpersisted RequestLog with model, tokens, cost, cache status,
        latency, path, provider, prompt tag, and outcome all set consistently.
    """
    table = get_pricing_table()
    provider, model = _RNG.choices(_MODEL_POOL, weights=_MODEL_WEIGHTS)[0]

    # Production-scale context sizes so per-request cost lands in the
    # cents-to-dimes band real traffic produces (not fractions of a cent).
    prompt_tokens = _RNG.randint(800, 20000)
    completion_tokens = _RNG.randint(200, 4000)
    total_tokens = prompt_tokens + completion_tokens

    price = table.lookup(provider, model)
    cost_usd = price.cost(prompt_tokens, completion_tokens) if price is not None else 0.0

    path = _RNG.choices(_PATHS, weights=_PATH_WEIGHTS)[0]
    cached = path in _CACHED_PATHS

    outcome = _RNG.choices(_OUTCOMES, weights=_OUTCOME_WEIGHTS)[0]

    # Latency: cache hits are fast and never touch the provider; streamed
    # requests carry a time-to-first-token; non-streamed ones do not.
    if cached:
        duration_ms = float(_RNG.randint(3, 40))
        provider_ms = None
        ttft_ms = None
    else:
        provider_ms = float(_RNG.randint(220, 3200))
        duration_ms = provider_ms + _RNG.randint(5, 60)
        ttft_ms = float(_RNG.randint(120, 900)) if path == "stream" else None

    # Tag ~40% of requests with a prompt so per-prompt aggregates populate.
    prompt_name: str | None = None
    prompt_version_num: int | None = None
    if _RNG.random() < 0.4:
        prompt_name, prompt_version_num = _RNG.choice(_PROMPT_TAGS)

    response_id = "resp_" + secrets.token_hex(12)
    cache_key = secrets.token_hex(16) if cached else None

    return RequestLog(
        created_at=created_at,
        key_id=key_id,
        account_id=account_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        cached=cached,
        cache_key=cache_key,
        response_id=response_id,
        prompt_name=prompt_name,
        prompt_version_num=prompt_version_num,
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
        path=path,
        outcome=outcome,
        provider=provider,
    )


# Rough per-day request volume per account. A moderate, bounded number so the
# charts read as busy without generating tens of thousands of rows.
_DAILY_VOLUME = 8
_HISTORY_DAYS = 30


async def seed_request_history(session: AsyncSession, tenants: list[SeededAccount]) -> int:
    """Generate ~30 days of request_logs for each keyed account that has none yet.

    Each account gets a bounded spread of requests across the last
    `_HISTORY_DAYS` days - a realistic mix of models, providers, cache hits,
    latency, outcomes, and prompt tags - so every dashboard panel is populated.
    Absolute spend is whatever this volume produces; the budget caps are
    derived from it afterwards (see `seed_budgets`), so the fill ratios are
    controlled independently of the dollar amounts.

    Args:
        session: Active async DB session.
        tenants: Seeded accounts; those without keys are skipped.

    Returns:
        The total number of request_logs rows inserted across all accounts.
    """
    now = datetime.now(UTC)
    total_inserted = 0

    for tenant in tenants:
        keys = list(
            (await session.execute(select(ApiKey).where(ApiKey.account_id == tenant.account.id)))
            .scalars()
            .all()
        )
        if not keys:
            continue
        if await _account_has_history(session, tenant.account.id):
            continue

        rows: list[RequestLog] = []
        for day_offset in range(_HISTORY_DAYS, 0, -1):
            day = now - timedelta(days=day_offset)
            for _ in range(_RNG.randint(_DAILY_VOLUME - 3, _DAILY_VOLUME + 4)):
                ts = day.replace(
                    hour=_RNG.randint(0, 23),
                    minute=_RNG.randint(0, 59),
                    second=_RNG.randint(0, 59),
                    microsecond=0,
                )
                rows.append(
                    _make_request_log(
                        account_id=tenant.account.id,
                        key_id=_RNG.choice(keys).id,
                        created_at=ts,
                    )
                )

        session.add_all(rows)
        await session.commit()
        total_inserted += len(rows)

    return total_inserted


async def _account_has_history(session: AsyncSession, account_id: int) -> bool:
    """Return whether an account already has any request_logs rows."""
    row = (
        await session.execute(
            select(RequestLog.id).where(RequestLog.account_id == account_id).limit(1)
        )
    ).first()
    return row is not None


async def clear_budget_counters() -> int:
    """Delete stale Redis budget counters so budget spend reflects the seeded DB.

    The seeder writes request_logs straight to the database, bypassing
    `budget.record_spend`, so any `budget:spend` counter Redis still holds is
    stale. `budget.get_period_spend` reads Redis first and only falls back to
    the DB aggregate when the key is absent, so those counters must be cleared
    for the dashboard budget cards and account spend figures to reflect the
    seeded traffic. Deletes every `budget:spend:*` and `budget:alerted:*` key
    (dev Redis only); each re-seeds itself from the DB on the next read.

    Returns:
        The number of Redis keys deleted.
    """
    redis = get_redis()
    deleted = 0
    for pattern in ("budget:spend:*", "budget:alerted:*"):
        async for key in redis.scan_iter(match=pattern):
            await redis.delete(key)
            deleted += 1
    await redis.aclose()
    return deleted


def _nice_ceil(value: float, step: float) -> float:
    """Round `value` up to the next multiple of `step` (a tidy budget figure)."""
    return math.ceil(value / step) * step


async def seed_budgets(
    session: AsyncSession, accounts: list[SeededAccount], *, operator: Account
) -> None:
    """Set each ratio'd account's monthly budget from its actual current-month spend.

    For an account with a `budget_ratio`, the cap is `spend / ratio` rounded up
    to a tidy figure, so the budget-card fill lands near that ratio (e.g.
    initech ~90%, near cap) regardless of the absolute dollars the generated
    traffic happened to produce. Accounts without a ratio keep the budget they
    were created with (a fixed cap, or unlimited). A no-op on any account whose
    current-month billed spend is zero (nothing to base a cap on), and on any
    account whose computed budget matches what it already has - which makes a
    re-run's audit trail quiet, same as every other seeding step here.

    Args:
        session: Active async DB session.
        accounts: Seeded accounts to consider; only those with a ratio are set.
        operator: Recorded as the actor on each `account.update` audit event,
            matching the real dashboard's operator-only budget endpoint.
    """
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for acct in accounts:
        if acct.budget_ratio is None:
            continue
        billed = (
            await session.execute(
                select(func.coalesce(func.sum(RequestLog.cost_usd), 0.0)).where(
                    RequestLog.account_id == acct.account.id,
                    RequestLog.cached.is_(False),
                    RequestLog.created_at >= month_start,
                )
            )
        ).scalar_one()
        if not billed:
            continue
        # Round to a tidy figure fine enough to preserve the target ratio at
        # these dollar amounts: a near-cap account rounds to the nearest $1 (so
        # a 0.9 ratio stays ~90%), a moderate one to the nearest $5.
        step = 1.0 if acct.budget_ratio >= 0.75 else 5.0
        budget = _nice_ceil(billed / acct.budget_ratio, step)
        if budget == acct.account.monthly_budget_usd:
            continue
        await account_service.set_budget(session, acct.account.id, budget)
        await record_audit_event(
            session,
            actor_account_id=operator.id,
            actor_label=operator.name,
            action="account.update",
            entity_type="account",
            entity_ref=acct.account.name,
            result="success",
            details={"monthly_budget_usd": budget},
        )


def _print_summary(seeded: list[SeededAccount], password: str, history_rows: int) -> None:
    """Print the login credentials, raw keys, and counts a developer needs next."""
    bar = "━" * 60
    print("\n✅ Development database seeded.\n")
    print(bar)
    print(f"Dashboard logins (password for all: {password})")
    print(bar)
    for s in seeded:
        if s.email is None:
            role = "pending signup (no login)" if s.account.status == "pending" else "no login"
            print(f"  {s.account.name:<16} {role}")
            continue
        role = "operator" if s.account.is_operator else "tenant"
        print(f"  {s.email:<26} {role:<9} account={s.account.name}")
    print(bar)
    print("API keys (shown once - copy them now)")
    print(bar)
    any_keys = False
    for s in seeded:
        for key_name, raw in s.keys:
            any_keys = True
            print(f"  {s.account.name}/{key_name}: {raw}")
    if not any_keys:
        print("  (no new keys minted - keys already existed)")
    print(bar)
    print(f"request_logs rows inserted: {history_rows}")
    print("\nDashboard:  http://localhost:8100/dashboard")
    print("Signup UI:  http://localhost:5173\n")


async def main(*, reset: bool, password: str, yes: bool) -> None:
    """Seed the database, optionally wiping seed-owned tables first.

    Args:
        reset: When True, TRUNCATE the seed-owned tables before populating.
        password: Shared dashboard-login password for seeded accounts.
        yes: When True, skip the interactive reset confirmation (still
            subject to the `_SAFE_RESET_HOSTS` hostname check).
    """
    if reset:
        database_url = get_settings().database_url
        _assert_reset_target_is_safe(database_url)
        if not yes:
            _confirm_reset(database_url)

    async with SessionLocal() as session:
        if reset:
            print("🧨 Resetting seed-owned tables...")
            await reset_seed_tables(session)

        print("👤 Seeding accounts, logins, and keys...")
        seeded = await seed_accounts(session, password)
        operator = next(s.account for s in seeded if s.account.is_operator)

        print("📝 Seeding prompts, versions, evals, curation, and samples...")
        # The operator account also gets a key and can carry history; include
        # every approved/keyed account when generating request traffic.
        keyed = [s for s in seeded if s.keys or s.account.status == "approved"]
        await seed_prompts(session, keyed, operator=operator)

        print("📊 Seeding request history...")
        history_rows = await seed_request_history(session, keyed)

        print("💰 Deriving budgets from generated spend...")
        await seed_budgets(session, seeded, operator=operator)
        for s in seeded:
            await session.refresh(s.account)

    print("🧹 Clearing stale Redis budget counters...")
    await clear_budget_counters()

    _print_summary(seeded, password, history_rows)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the seeder."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--reset",
        action="store_true",
        help="TRUNCATE seed-owned tables before populating (fresh rebuild)",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="skip the interactive --reset confirmation prompt (e.g. for scripted use)",
    )
    parser.add_argument(
        "--password",
        default=DEFAULT_PASSWORD,
        help=f"shared dashboard-login password (default: {DEFAULT_PASSWORD})",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(reset=args.reset, password=args.password, yes=args.yes))
