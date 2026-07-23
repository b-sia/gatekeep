from __future__ import annotations

import pytest

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.cli import _eval_run, _set_budget, main
from gatekeep.evals import add_case, create_suite
from gatekeep.models import ApiKey
from gatekeep.prompts import create_prompt
from gatekeep.providers.base import CompletionResult


class _FakeProvider:
    """Fake provider that returns queued text responses in order, ignoring the payload."""

    def __init__(self, texts):
        self._texts = list(texts)

    async def complete(self, payload):
        """Pop and return the next queued text as a CompletionResult."""
        return CompletionResult(
            text=self._texts.pop(0), input_tokens=1, output_tokens=1, stop_reason="stop"
        )


def _patch_provider(monkeypatch, texts):
    """Make `gatekeep.cli.AnthropicProvider(...)` return a `_FakeProvider` regardless of args.

    This keeps `_eval_run`-level tests from making real Anthropic API calls, without
    needing to touch the DB session/event-loop plumbing that `main()` sets up.
    """
    monkeypatch.setattr(
        "gatekeep.cli.AnthropicProvider", lambda client: _FakeProvider(texts)
    )


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


# --- key set-budget: does it set/clear monthly_budget_usd? -----------------------


async def test_set_budget_sets_amount_on_existing_key(session):
    raw = generate_key()
    session.add(ApiKey(name="budget-key", key_hash=hash_key(raw)))
    await session.commit()

    await _set_budget("budget-key", 25.0, unlimited=False)

    session.expire_all()
    key = await session.get(ApiKey, 1)
    assert key.monthly_budget_usd == 25.0


async def test_set_budget_unlimited_clears_amount(session):
    raw = generate_key()
    session.add(
        ApiKey(name="budget-key", key_hash=hash_key(raw), monthly_budget_usd=10.0)
    )
    await session.commit()

    await _set_budget("budget-key", None, unlimited=True)

    session.expire_all()
    key = await session.get(ApiKey, 1)
    assert key.monthly_budget_usd is None


async def test_set_budget_raises_for_unknown_key_name():
    with pytest.raises(ValueError, match="no API key named"):
        await _set_budget("does-not-exist", 5.0, unlimited=False)


async def test_set_budget_raises_when_neither_amount_nor_unlimited_given(session):
    raw = generate_key()
    session.add(ApiKey(name="budget-key", key_hash=hash_key(raw)))
    await session.commit()

    with pytest.raises(ValueError, match="must provide an amount"):
        await _set_budget("budget-key", None, unlimited=False)


def test_main_key_set_budget_dispatches(monkeypatch):
    """`main(["key", "set-budget", ...])` must parse and dispatch to _set_budget."""
    calls = []

    async def _fake_set_budget(name, amount, unlimited):
        calls.append((name, amount, unlimited))

    monkeypatch.setattr("gatekeep.cli._set_budget", _fake_set_budget)

    code = main(["key", "set-budget", "some-key", "12.5"])

    assert code == 0
    assert calls == [("some-key", 12.5, False)]
