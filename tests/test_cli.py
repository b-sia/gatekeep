from __future__ import annotations

import pytest
from sqlalchemy import select

from gatekeep import account_service
from gatekeep.cli import (
    _account_create,
    _account_list,
    _account_set_budget,
    _account_set_operator,
    _clear_candidate,
    _eval_review,
    _eval_run,
    _key_create,
    _key_list,
    _list,
    _resolve_account_id,
    _set_candidate,
    _show,
    build_parser,
    main,
)
from gatekeep.evals import add_case, create_suite
from gatekeep.models import Account, ApiKey
from gatekeep.prompts import add_prompt_version, create_prompt
from tests.helpers import FakeProvider as _FakeProvider
from tests.helpers import create_account


def _patch_provider(monkeypatch, texts):
    """Make `gatekeep.cli.AnthropicProvider(...)` return a `_FakeProvider` regardless of args.

    This keeps `_eval_run`-level tests from making real Anthropic API calls, without
    needing to touch the DB session/event-loop plumbing that `main()` sets up.
    """
    monkeypatch.setattr("gatekeep.cli.AnthropicProvider", lambda client: _FakeProvider(texts))


# --- _eval_run: does it correctly report pass/fail? -------------------------------


async def test_eval_run_returns_false_when_suite_fails(session, monkeypatch):
    """Regression test for the Critical bug: `_eval_run` used to return `None`
    unconditionally, so `gatekeep eval run` could never signal a failing suite to its
    caller (and thus CI could never gate on it). It must now return `run.passed`.
    """
    await create_prompt("system-context", "answer helpfully", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="__impossible_token_that_will_never_appear__",
    )
    _patch_provider(monkeypatch, ["this will not contain the expected token"])

    passed = await _eval_run("system-context", None, None, include_unreviewed=False)

    assert passed is False


async def test_eval_run_returns_true_when_suite_passes(session, monkeypatch):
    """A passing eval suite must return True."""
    await create_prompt("system-context", "answer helpfully", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains",
        expected="pong",
    )
    _patch_provider(monkeypatch, ["...pong..."])

    passed = await _eval_run("system-context", None, None, include_unreviewed=False)

    assert passed is True


# --- main(): does it translate _eval_run's result into the right exit code? -------


def test_main_eval_run_returns_2_when_eval_run_reports_failure(monkeypatch):
    """`main()` must exit 2 (matching the promotion gate's "eval didn't pass"
    convention) when `_eval_run` reports a failing suite, not fall through to 0.
    """

    async def _fake_eval_run(name, version, model, include_unreviewed):
        return False

    monkeypatch.setattr("gatekeep.cli._eval_run", _fake_eval_run)

    code = main(["eval", "run", "system-context"])

    assert code == 2


def test_main_eval_run_returns_0_when_eval_run_reports_pass(monkeypatch):
    """A passing eval suite must still exit 0."""

    async def _fake_eval_run(name, version, model, include_unreviewed):
        return True

    monkeypatch.setattr("gatekeep.cli._eval_run", _fake_eval_run)

    code = main(["eval", "run", "system-context"])

    assert code == 0


def test_main_eval_run_returns_1_when_no_suite_registered(monkeypatch):
    """A prompt with no eval suite registered must still exit 1 (unchanged, existing
    behavior via the ValueError raised by `run_suite_for_prompt`), not the new 2 used
    for a genuine eval failure.
    """

    async def _fake_eval_run(name, version, model, include_unreviewed):
        raise ValueError(f"no eval suite registered for prompt {name!r}")

    monkeypatch.setattr("gatekeep.cli._eval_run", _fake_eval_run)

    code = main(["eval", "run", "some-nonexistent-prompt-name"])

    assert code == 1


# --- prompt set-candidate / clear-candidate ----------------------------------


async def test_set_candidate_configures_prompt(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)

    await _set_candidate("system-context", 2, 25.0)

    from gatekeep.prompts import get_active_prompt_version

    # active version is unaffected by setting a candidate
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 1


async def test_clear_candidate_removes_configured_candidate(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await _set_candidate("system-context", 2, 25.0)

    await _clear_candidate("system-context")


def test_build_parser_accepts_set_candidate_and_clear_candidate():
    parser = build_parser()

    args = parser.parse_args(["prompt", "set-candidate", "system-context", "2", "--pct", "25"])
    assert args.prompt_command == "set-candidate"
    assert args.name == "system-context"
    assert args.version == 2
    assert args.pct == 25.0

    args = parser.parse_args(["prompt", "clear-candidate", "system-context"])
    assert args.prompt_command == "clear-candidate"
    assert args.name == "system-context"


def test_main_set_candidate_rejects_out_of_range_pct(monkeypatch):
    """main() must surface set_candidate_version's ValueError as exit code 1,
    same as the other prompt-management ValueErrors."""

    async def _fake_set_candidate(name, version, pct):
        raise ValueError("traffic_pct must be between 0 and 100, got 150.0")

    monkeypatch.setattr("gatekeep.cli._set_candidate", _fake_set_candidate)

    code = main(["prompt", "set-candidate", "system-context", "2", "--pct", "150"])

    assert code == 1


# --- _eval_review: does the edit path handle quit correctly? ----------------------


async def test_eval_review_edit_then_quit_does_not_delete_the_case(session, monkeypatch):
    """Regression test: typing `q` at the post-edit "approve?" prompt used to fall
    through to `approve=(answer == "y")` (False), which deletes the case instead of
    quitting the review loop. Editing then quitting must leave the case in place,
    still unreviewed, with the edited criteria kept.
    """
    await create_prompt("system-context", "answer helpfully", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    case = await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="llm_judge",
        judge_criteria="original criteria",
        reviewed=False,
        source="curated",
    )

    answers = iter(["e", "edited criteria", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    await _eval_review("system-context")

    await session.refresh(case)
    assert case.judge_criteria == "edited criteria"
    assert case.reviewed is False


# --- account set-budget: does it set/clear the account's monthly_budget_usd? -----


async def test_account_set_budget_sets_amount_on_existing_account(session):
    """`account set-budget <name> <amount>` sets the account's monthly_budget_usd."""
    account = await create_account(session, name="budget-account")
    await session.commit()
    account_id = account.id

    await _account_set_budget("budget-account", 25.0, unlimited=False)

    session.expire_all()
    refreshed = await session.get(Account, account_id)
    assert refreshed.monthly_budget_usd == 25.0


async def test_account_set_budget_unlimited_clears_amount(session):
    """`account set-budget --unlimited` clears a previously set cap."""
    account = await create_account(session, name="budget-account", monthly_budget_usd=10.0)
    await session.commit()
    account_id = account.id

    await _account_set_budget("budget-account", None, unlimited=True)

    session.expire_all()
    refreshed = await session.get(Account, account_id)
    assert refreshed.monthly_budget_usd is None


async def test_account_set_budget_raises_for_unknown_account_name():
    """A name matching no account raises rather than silently no-op'ing."""
    with pytest.raises(ValueError, match="no account named"):
        await _account_set_budget("does-not-exist", 5.0, unlimited=False)


async def test_account_set_budget_raises_when_neither_amount_nor_unlimited_given(session):
    """Calling with neither an amount nor --unlimited is rejected up front."""
    await create_account(session, name="budget-account")
    await session.commit()

    with pytest.raises(ValueError, match="must provide an amount"):
        await _account_set_budget("budget-account", None, unlimited=False)


async def test_account_set_budget_raises_for_non_positive_amount(session):
    """A non-positive amount is rejected by the underlying account_service call."""
    await create_account(session, name="budget-account")
    await session.commit()

    with pytest.raises(account_service.InvalidBudgetError, match="amount must be positive"):
        await _account_set_budget("budget-account", -5.0, unlimited=False)
    with pytest.raises(account_service.InvalidBudgetError, match="amount must be positive"):
        await _account_set_budget("budget-account", 0.0, unlimited=False)


def test_main_account_set_budget_dispatches(monkeypatch):
    """`main(["account", "set-budget", ...])` must parse and dispatch to _account_set_budget."""
    calls = []

    async def _fake_set_budget(name, amount, unlimited):
        """Record the call instead of touching the DB."""
        calls.append((name, amount, unlimited))

    monkeypatch.setattr("gatekeep.cli._account_set_budget", _fake_set_budget)

    code = main(["account", "set-budget", "some-account", "12.5"])

    assert code == 0
    assert calls == [("some-account", 12.5, False)]


def test_key_set_budget_removed():
    """`key set-budget` no longer parses; budget moved to `account set-budget`."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["key", "set-budget", "team-k", "10"])


# --- account create / resolve / set-operator / list --------------------------


async def test_account_create_and_resolve(session, capsys):
    """`account create` makes an account resolvable by name."""
    await _account_create("team-a", budget=None, unlimited=False, operator=False)
    account_id = await _resolve_account_id(session, "team-a")
    assert account_id is not None


async def test_account_set_operator_bootstrap(session):
    """`account set-operator` promotes an account (the headless bootstrap path)."""
    await _account_create("boot", budget=None, unlimited=False, operator=False)
    await _account_set_operator("boot", off=False)
    account_id = await _resolve_account_id(session, "boot")

    assert (await session.get(Account, account_id)).is_operator is True


async def test_account_list_includes_created_account(session, capsys):
    """`account list` prints every account by name."""
    await _account_create("team-list", budget=None, unlimited=False, operator=False)

    await _account_list()

    out = capsys.readouterr().out
    assert "team-list" in out


# --- key create / list --------------------------------------------------------


async def test_key_create_prints_raw_and_persists(session, capsys):
    """`key create` mints a key, prints the raw value, and stores its hash."""
    await _account_create("team-k", budget=None, unlimited=False, operator=False)
    await _key_create("team-k", "prod")
    printed = capsys.readouterr().out
    assert "gk-" in printed
    account_id = await _resolve_account_id(session, "team-k")
    keys = (
        (await session.execute(select(ApiKey).where(ApiKey.account_id == account_id)))
        .scalars()
        .all()
    )
    assert [k.name for k in keys] == ["prod"]


async def test_key_list_reports_created_key(session, capsys):
    """`key list` prints an account's keys with their active/revoked status."""
    await _account_create("team-kl", budget=None, unlimited=False, operator=False)
    await _key_create("team-kl", "prod")
    capsys.readouterr()  # discard the printed raw key

    await _key_list("team-kl")

    out = capsys.readouterr().out
    assert "prod\tactive" in out


# --- candidate visibility: no candidate vs. candidate paused at 0% -----------


async def test_show_reports_no_candidate_suffix_when_none_configured(session, capsys):
    await create_prompt("system-context", "v1", session)

    await _show("system-context")

    out = capsys.readouterr().out
    assert "candidate" not in out


async def test_show_reports_candidate_paused_at_zero_pct_distinctly(session, capsys):
    """A candidate configured at 0% traffic (a paused rollout) must be visibly
    distinct from no candidate being configured at all - both route 100% of
    traffic to the active version, but they are not the same state."""
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await _set_candidate("system-context", 2, 0.0)

    await _show("system-context")

    out = capsys.readouterr().out
    assert "candidate: v2 @ 0.0% (paused)" in out


async def test_show_reports_candidate_at_nonzero_pct_without_paused_label(session, capsys):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    await _set_candidate("system-context", 2, 25.0)

    await _show("system-context")

    out = capsys.readouterr().out
    assert "candidate: v2 @ 25.0%" in out
    assert "paused" not in out


async def test_list_reports_candidate_state_per_prompt(session, capsys):
    await create_prompt("no-candidate-prompt", "v1", session)
    await create_prompt("paused-candidate-prompt", "v1", session)
    await add_prompt_version("paused-candidate-prompt", "v2 text", session)
    await _set_candidate("paused-candidate-prompt", 2, 0.0)

    await _list()

    out = capsys.readouterr().out
    assert "no-candidate-prompt\tv1\n" in out
    assert "paused-candidate-prompt\tv1 (candidate: v2 @ 0.0% (paused))" in out
