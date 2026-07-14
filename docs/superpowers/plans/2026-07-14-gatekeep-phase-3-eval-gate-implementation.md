# Gatekeep Phase 3 - Eval Gate & Prompt Quality Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate prompt promotion behind an automated eval suite, grow that suite from real traffic, run it in CI, and route to the cheapest model that clears a measured quality bar.

**Architecture:** New Postgres tables (`request_samples`, `eval_suites`, `eval_cases`, `eval_runs`) plus two columns on `request_logs`. `gatekeep/evals.py` renders a prompt version against each case, scores it (exact / contains / fixed-Sonnet LLM judge), persists an `EvalRun`, and exposes a gate callable that `promote_prompt` invokes before flipping the active pointer. Curation mines a dedicated `request_samples` corpUse subus (written on the cache-miss path, decoupled from cache invalidation) into unreviewed cases. Prompt templates become in-repo files under `prompts/` so a PR is the natural place for both human review and the CI gate. Cost routing reads per-model `EvalRun` history.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 async, asyncpg, Alembic, Anthropic SDK (async), Redis, pytest + pytest-asyncio (`asyncio_mode = auto`), ruff.

## Global Constraints

- **Design decisions are locked** in `docs/superpowers/plans/2026-07-13-gatekeep-phase-3-eval-gate.md`: Q1 = in-repo prompt files; Q2 = fixed stronger judge (always `claude-sonnet-5`, independent of the model under test); Q3 = per-suite `pass_threshold` defaulted from a gateway-wide setting; Q4 = dedicated `request_samples` corpus for curation (not `request_logs`, not `cached_responses`).
- **Migrations are hand-authored with sequential integer revision ids**, matching `migrations/versions/0001`..`0006` - do NOT use `alembic revision --autogenerate`. The next revision is `0007`, `down_revision = "0006"`.
- **No em dash** anywhere (use a plain `-`). Global user rule.
- **Every function, method, and class gets a docstring** stating purpose, params, returns, and raised exceptions where applicable. Global user rule.
- **Prompt templates have no placeholder substitution** in this codebase (Phase 2 prepends the template text verbatim as a system message; see `app.py:147`). "Render a template against a case" therefore means: put the template text in the payload `system` field and the case's `input_messages` in `messages`. Do NOT add `str.format`/Jinja substitution.
- **JSON columns use `JSONB`** (`from sqlalchemy.dialects.postgresql import JSONB`).
- **Timestamps use the existing `_utcnow` helper** in `gatekeep/models.py` for `default=`, and `server_default=sa.func.now()` in migrations, matching current models.
- **Provider calls are dependency-injected** into `run_eval_suite` (duck-typed `async complete(payload) -> CompletionResult`) so tests use a fake and never hit the network. Only the CLI/app wiring constructs a real `AnthropicProvider`.
- **Tests run against real Postgres + Redis** via `tests/conftest.py` (it drops/recreates `Base.metadata` per test). New models must import cleanly so their tables register on `Base.metadata`.
- **DRY, YAGNI, TDD, frequent commits.** One commit per task minimum.

---

## File Structure

**New source files:**
- `gatekeep/evals.py` - suite/case CRUD helpers, `run_eval_suite`, scoring, `EvalGateFailure`, `make_eval_gate`.
- `gatekeep/samples.py` - `record_request_sample` (write on cache-miss path) + `recent_samples` (read for curation).
- `gatekeep/curation.py` - turn `request_samples` into unreviewed `EvalCase` rows; list/approve/reject.
- `gatekeep/routing.py` - `select_model` cost-based selection.
- `migrations/versions/0007_eval_tables.py` - all new tables + `request_logs` columns.
- `prompts/` - in-repo prompt template files (decided Q1).
- `.github/workflows/eval-gate.yml`, `scripts/ci-eval-check.sh` - CI gate.

**Modified source files:**
- `gatekeep/models.py` - `RequestSample`, `EvalSuite`, `EvalCase`, `EvalRun`; add `prompt_name` + `routed_from` to `RequestLog`.
- `gatekeep/config.py` - `eval_judge_model`, `eval_pass_threshold_default`.
- `gatekeep/prompts.py` - `promote_prompt` gains an optional `gate` callable.
- `gatekeep/app.py` - record a sample on the cache-miss path; optional cost-routing step; thread `prompt_name` into `log_request`.
- `gatekeep/accounting.py` - `log_request` accepts `prompt_name` and `routed_from`.
- `gatekeep/cli.py` - `eval` subcommands and `prompt sync`; surface `EvalGateFailure`.
- `README.md` - eval gate + curation workflow.

**New test files:** `tests/test_eval_models.py`, `tests/test_samples.py`, `tests/test_evals.py`, `tests/test_curation.py`, `tests/test_routing.py`, `tests/test_prompt_sync.py`; additions to `tests/test_prompts.py`.

---

## Task 1: Schema, migration, and settings

**Files:**
- Modify: `gatekeep/models.py`
- Modify: `gatekeep/config.py`
- Create: `migrations/versions/0007_eval_tables.py`
- Test: `tests/test_eval_models.py`

**Interfaces:**
- Consumes: `gatekeep.db.Base`, existing `RequestLog`, `_utcnow`.
- Produces (relied on by every later task):
  - `RequestSample(id, created_at, key_id, prompt_name: str|None, model: str, input_messages: list[dict], output_text: str)`
  - `EvalSuite(id, name: str, prompt_name: str (unique), pass_threshold: float, created_at)`
  - `EvalCase(id, suite_id, input_messages: list[dict], expected: str|None, check_type: str, judge_criteria: str|None, reviewed: bool, source: str, created_at)`
  - `EvalRun(id, suite_id, prompt_version_id, model: str, score: float, passed: bool, report: list[dict], created_at)`
  - `RequestLog.prompt_name: str|None`, `RequestLog.routed_from: str|None`
  - `Settings.eval_judge_model: str = "claude-sonnet-5"`, `Settings.eval_pass_threshold_default: float = 0.9`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eval_models.py`:

```python
from sqlalchemy import select

from gatekeep.models import (
    ApiKey,
    EvalCase,
    EvalRun,
    EvalSuite,
    Prompt,
    PromptVersion,
    RequestSample,
)


async def test_request_sample_round_trips_structured_messages(session):
    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    sample = RequestSample(
        key_id=key.id,
        prompt_name="system-context",
        model="claude-sonnet-5",
        input_messages=[{"role": "user", "content": "hi"}],
        output_text="hello",
    )
    session.add(sample)
    await session.commit()

    got = (await session.execute(select(RequestSample))).scalar_one()
    assert got.input_messages == [{"role": "user", "content": "hi"}]
    assert got.prompt_name == "system-context"


async def test_eval_suite_case_and_run_persist(session):
    suite = EvalSuite(name="system-context", prompt_name="system-context", pass_threshold=0.9)
    session.add(suite)
    await session.flush()

    case = EvalCase(
        suite_id=suite.id,
        input_messages=[{"role": "user", "content": "ping"}],
        expected="pong",
        check_type="contains",
        reviewed=True,
        source="manual",
    )
    session.add(case)

    prompt = Prompt(name="system-context")
    session.add(prompt)
    await session.flush()
    version = PromptVersion(prompt_id=prompt.id, version_num=1, template="t", active=True)
    session.add(version)
    await session.flush()

    run = EvalRun(
        suite_id=suite.id,
        prompt_version_id=version.id,
        model="claude-sonnet-5",
        score=1.0,
        passed=True,
        report=[{"case_id": case.id, "passed": True, "actual_output": "pong", "reason": ""}],
    )
    session.add(run)
    await session.commit()

    assert (await session.execute(select(EvalCase))).scalar_one().check_type == "contains"
    assert (await session.execute(select(EvalRun))).scalar_one().passed is True


async def test_request_log_has_prompt_name_and_routed_from(session):
    from gatekeep.models import RequestLog

    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    log = RequestLog(
        key_id=key.id,
        model="claude-haiku-4-5-20251001",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost_usd=0.0,
        response_id="r1",
        prompt_name="system-context",
        routed_from="claude-sonnet-5",
    )
    session.add(log)
    await session.commit()
    got = (await session.execute(select(RequestLog))).scalar_one()
    assert got.prompt_name == "system-context"
    assert got.routed_from == "claude-sonnet-5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_eval_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'RequestSample'` (or `EvalSuite`).

- [ ] **Step 3: Add the models**

In `gatekeep/models.py`, add the `JSONB` import near the top imports:

```python
from sqlalchemy.dialects.postgresql import JSONB
```

Add two columns to `RequestLog` (after `response_id`):

```python
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    routed_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

Append the new models at the end of the file:

```python
class RequestSample(Base):
    """A durable, append-only sample of one cache-miss request's content.

    Written on the provider-served (cache-miss) path so curation has a
    representative corpus of real traffic per prompt_name. Deliberately
    decoupled from cached_responses (which is deduped and deleted on every
    prompt promotion) and from request_logs (which stores no message content).
    """

    __tablename__ = "request_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), nullable=False)
    prompt_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_messages: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)


class EvalSuite(Base):
    """A per-prompt eval suite; a prompt version must clear it before promotion."""

    __tablename__ = "eval_suites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pass_threshold: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EvalCase(Base):
    """One scored eval case: an input, a check type, and its expected result or judge criteria."""

    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id"), nullable=False)
    input_messages: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_type: Mapped[str] = mapped_column(String(32), nullable=False)
    judge_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EvalRun(Base):
    """One execution of a suite against a specific prompt version and model."""

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    suite_id: Mapped[int] = mapped_column(ForeignKey("eval_suites.id"), nullable=False)
    prompt_version_id: Mapped[int] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    report: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
```

- [ ] **Step 4: Add the settings**

In `gatekeep/config.py`, inside `Settings` (after `semantic_cache_similarity_threshold`):

```python
    eval_judge_model: str = "claude-sonnet-5"
    eval_pass_threshold_default: float = 0.9
```

- [ ] **Step 5: Run the model test to verify it passes**

Run: `pytest tests/test_eval_models.py -v`
Expected: PASS (3 passed). The conftest builds the schema from `Base.metadata`, so no migration is needed for tests.

- [ ] **Step 6: Hand-author the migration**

Create `migrations/versions/0007_eval_tables.py`:

```python
"""eval gate tables + request-sample corpus + request_logs prompt/routing columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_logs",
        sa.Column("prompt_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "request_logs",
        sa.Column("routed_from", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "request_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("prompt_name", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_messages", postgresql.JSONB(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
    )
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("prompt_name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("pass_threshold", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False
        ),
        sa.Column("input_messages", postgresql.JSONB(), nullable=False),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("check_type", sa.String(length=32), nullable=False),
        sa.Column("judge_criteria", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="manual"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False
        ),
        sa.Column(
            "prompt_version_id",
            sa.Integer(),
            sa.ForeignKey("prompt_versions.id"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
    op.drop_table("eval_suites")
    op.drop_table("request_samples")
    op.drop_column("request_logs", "routed_from")
    op.drop_column("request_logs", "prompt_name")
```

- [ ] **Step 7: Verify the migration applies cleanly on a real DB**

Run: `alembic upgrade head && alembic downgrade 0006 && alembic upgrade head`
Expected: no errors; `0007` applies, reverts, and re-applies.

- [ ] **Step 8: Commit**

```bash
git add gatekeep/models.py gatekeep/config.py migrations/versions/0007_eval_tables.py tests/test_eval_models.py
git commit -m "feat(evals): add eval + request-sample schema, migration, and settings"
```

---

## Task 2: request_samples writer wired into the cache-miss path

**Files:**
- Create: `gatekeep/samples.py`
- Modify: `gatekeep/accounting.py` (thread `prompt_name`/`routed_from` into `log_request`)
- Modify: `gatekeep/app.py` (record a sample on the cache-miss branch; pass `prompt_name` to `log_request`)
- Test: `tests/test_samples.py`

**Interfaces:**
- Consumes: `RequestSample`, `AsyncSession`.
- Produces:
  - `async def record_request_sample(session, *, key_id: int, prompt_name: str, model: str, input_messages: list[dict], output_text: str) -> RequestSample`
  - `async def recent_samples(prompt_name: str, session: AsyncSession, *, limit: int) -> list[RequestSample]`
  - `log_request(..., prompt_name: str | None = None, routed_from: str | None = None)`

- [ ] **Step 1: Write the failing test**

Create `tests/test_samples.py`:

```python
from gatekeep.models import ApiKey
from gatekeep.samples import recent_samples, record_request_sample


async def _key(session):
    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    return key


async def test_record_and_read_recent_samples_newest_first(session):
    key = await _key(session)
    for i in range(3):
        await record_request_sample(
            session,
            key_id=key.id,
            prompt_name="p",
            model="claude-sonnet-5",
            input_messages=[{"role": "user", "content": f"m{i}"}],
            output_text=f"o{i}",
        )

    got = await recent_samples("p", session, limit=2)
    assert [s.output_text for s in got] == ["o2", "o1"]


async def test_recent_samples_filters_by_prompt_name(session):
    key = await _key(session)
    await record_request_sample(
        session, key_id=key.id, prompt_name="a", model="m",
        input_messages=[{"role": "user", "content": "x"}], output_text="ox",
    )
    await record_request_sample(
        session, key_id=key.id, prompt_name="b", model="m",
        input_messages=[{"role": "user", "content": "y"}], output_text="oy",
    )
    got = await recent_samples("a", session, limit=10)
    assert [s.output_text for s in got] == ["ox"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_samples.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.samples'`.

- [ ] **Step 3: Implement the sample module**

Create `gatekeep/samples.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import RequestSample


async def record_request_sample(
    session: AsyncSession,
    *,
    key_id: int,
    prompt_name: str,
    model: str,
    input_messages: list[dict],
    output_text: str,
) -> RequestSample:
    """Persist one cache-miss request as a RequestSample and commit it.

    Only called for prompt-scoped, provider-served requests so the corpus
    stays a representative, append-only record of fresh traffic per prompt.
    """
    sample = RequestSample(
        key_id=key_id,
        prompt_name=prompt_name,
        model=model,
        input_messages=input_messages,
        output_text=output_text,
    )
    session.add(sample)
    await session.commit()
    return sample


async def recent_samples(
    prompt_name: str, session: AsyncSession, *, limit: int
) -> list[RequestSample]:
    """Return the most recent `limit` request samples for `prompt_name`, newest first."""
    result = await session.execute(
        select(RequestSample)
        .where(RequestSample.prompt_name == prompt_name)
        .order_by(RequestSample.created_at.desc(), RequestSample.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_samples.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Thread prompt_name/routed_from through log_request**

In `gatekeep/accounting.py`, extend `log_request`'s signature and the `RequestLog(...)` construction:

```python
async def log_request(
    session: AsyncSession,
    *,
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_id: str,
    cached: bool = False,
    cache_key: str | None = None,
    cost_usd_override: float | None = None,
    prompt_name: str | None = None,
    routed_from: str | None = None,
) -> RequestLog:
```

and add to the `RequestLog(...)` call:

```python
        prompt_name=prompt_name,
        routed_from=routed_from,
```

Update the docstring's parameter description to mention `prompt_name`/`routed_from` are request-level metadata, defaulting to None.

- [ ] **Step 6: Record a sample on the cache-miss branch in app.py**

In `gatekeep/app.py`, add the import near the other `gatekeep` imports:

```python
from gatekeep.samples import record_request_sample
```

In `chat_completions`, in the fresh-generation branch (right after the existing `store_cached_response(...)` block, before the final `log_request(...)`), add:

```python
    if req.prompt_name is not None:
        await record_request_sample(
            session,
            key_id=key.id,
            prompt_name=req.prompt_name,
            model=model,
            input_messages=payload["messages"],
            output_text=response.choices[0].message.content or "",
        )
```

Then pass `prompt_name=req.prompt_name` into the final non-streaming `log_request(...)` call so the accounting log also carries it.

> Note: `payload["messages"]` is exactly the user/assistant turns with the injected system template already lifted into `payload["system"]` by `openai_to_payload`. That is the correct input for an eval case: the template under test is NOT baked into the sample.

- [ ] **Step 7: Verify nothing regressed**

Run: `pytest tests/test_samples.py tests/test_endpoint.py tests/test_accounting.py -v`
Expected: PASS (all green).

- [ ] **Step 8: Commit**

```bash
git add gatekeep/samples.py gatekeep/accounting.py gatekeep/app.py tests/test_samples.py
git commit -m "feat(evals): record request samples on cache-miss path for curation corpus"
```

---

## Task 3: Eval runner, scoring, and suite/case CRUD

**Files:**
- Create: `gatekeep/evals.py`
- Test: `tests/test_evals.py`

**Interfaces:**
- Consumes: `EvalSuite`, `EvalCase`, `EvalRun`, `PromptVersion`, `Settings`; an injected provider with `async complete(payload) -> CompletionResult`.
- Produces:
  - `async def create_suite(prompt_name, session, *, pass_threshold, name=None) -> EvalSuite`
  - `async def get_suite_for_prompt(prompt_name, session) -> EvalSuite | None`
  - `async def add_case(suite_id, session, *, input_messages, check_type, expected=None, judge_criteria=None, reviewed=True, source="manual") -> EvalCase`
  - `async def run_eval_suite(suite, prompt_version, session, *, provider, generate_model, judge_model, max_tokens, include_unreviewed=False) -> EvalRun`
  - `class EvalGateFailure(Exception)` with attribute `.eval_run: EvalRun`
  - `def make_eval_gate(*, provider, generate_model, judge_model, max_tokens) -> Callable`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_evals.py`:

```python
import pytest

from gatekeep.evals import (
    EvalGateFailure,
    add_case,
    create_suite,
    get_suite_for_prompt,
    make_eval_gate,
    run_eval_suite,
)
from gatekeep.models import Prompt, PromptVersion
from gatekeep.providers.base import CompletionResult


class FakeProvider:
    """Provider stub returning queued texts in order, one per complete() call."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        return CompletionResult(
            text=self._texts.pop(0), input_tokens=1, output_tokens=1, stop_reason="stop"
        )


async def _prompt_version(session, template="answer helpfully"):
    prompt = Prompt(name="system-context")
    session.add(prompt)
    await session.flush()
    version = PromptVersion(
        prompt_id=prompt.id, version_num=1, template=template, active=True
    )
    session.add(version)
    await session.flush()
    return version


async def test_contains_check_scores_and_persists_report(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains", expected="pong",
    )
    version = await _prompt_version(session)
    provider = FakeProvider(["...pong..."])

    run = await run_eval_suite(
        suite, version, session,
        provider=provider, generate_model="claude-haiku-4-5-20251001",
        judge_model="claude-sonnet-5", max_tokens=64,
    )

    assert run.score == 1.0
    assert run.passed is True
    assert run.model == "claude-haiku-4-5-20251001"
    assert run.report[0]["passed"] is True
    # template went to system, case messages went to messages
    assert provider.payloads[0]["system"] == "answer helpfully"
    assert provider.payloads[0]["messages"] == [{"role": "user", "content": "ping"}]


async def test_llm_judge_uses_fixed_judge_model_and_parses_verdict(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "explain X"}],
        check_type="llm_judge", judge_criteria="on-topic and coherent",
    )
    version = await _prompt_version(session)
    # 1st call = generation, 2nd call = judge verdict
    provider = FakeProvider(["some answer", "PASS - it is on topic"])

    run = await run_eval_suite(
        suite, version, session,
        provider=provider, generate_model="claude-haiku-4-5-20251001",
        judge_model="claude-sonnet-5", max_tokens=64,
    )

    assert run.passed is True
    assert provider.payloads[1]["model"] == "claude-sonnet-5"  # fixed judge, not generate_model


async def test_failed_run_scores_below_threshold(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact", expected="pong",
    )
    version = await _prompt_version(session)
    provider = FakeProvider(["nope"])

    run = await run_eval_suite(
        suite, version, session,
        provider=provider, generate_model="m", judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    assert run.passed is False
    assert run.score == 0.0


async def test_gate_raises_eval_gate_failure_when_run_fails(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact", expected="pong",
    )
    version = await _prompt_version(session)
    gate = make_eval_gate(
        provider=FakeProvider(["wrong"]), generate_model="m",
        judge_model="claude-sonnet-5", max_tokens=64,
    )
    with pytest.raises(EvalGateFailure):
        await gate("system-context", version, session)


async def test_gate_is_noop_when_no_suite_registered(session):
    version = await _prompt_version(session)
    gate = make_eval_gate(
        provider=FakeProvider([]), generate_model="m",
        judge_model="claude-sonnet-5", max_tokens=64,
    )
    # no suite for this prompt -> gate returns without calling the provider
    await gate("system-context", version, session)
    assert await get_suite_for_prompt("no-suite", session) is None


async def test_unreviewed_cases_excluded_by_default(session):
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact", expected="pong", reviewed=False, source="curated",
    )
    version = await _prompt_version(session)
    provider = FakeProvider([])  # no reviewed cases -> no provider calls

    run = await run_eval_suite(
        suite, version, session,
        provider=provider, generate_model="m", judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    # empty suite of reviewed cases scores 1.0 (vacuously passes) and calls nothing
    assert run.report == []
    assert provider.payloads == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.evals'`.

- [ ] **Step 3: Implement the eval runner**

Create `gatekeep/evals.py`:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import EvalCase, EvalRun, EvalSuite, PromptVersion

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evals.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/evals.py tests/test_evals.py
git commit -m "feat(evals): eval runner, scoring (exact/contains/llm-judge), and promotion gate"
```

---

## Task 4: Wire the gate into promotion

**Files:**
- Modify: `gatekeep/prompts.py` (`promote_prompt` gains an optional `gate`)
- Modify: `gatekeep/cli.py` (`_promote` builds the real gate; surface `EvalGateFailure`)
- Test: `tests/test_prompts.py` (add gate regression cases)

**Interfaces:**
- Consumes: `gatekeep.evals.Gate`, `make_eval_gate`, `EvalGateFailure`.
- Produces: `promote_prompt(name, version_num, session, *, redis=None, gate=None)` - when `gate` is provided it is awaited with `(name, target_version, session)` before the pointer flip; a raised `EvalGateFailure` aborts promotion untouched.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_prompts.py`:

```python
from gatekeep.evals import EvalGateFailure, add_case, create_suite
from gatekeep.models import Prompt as _Prompt  # noqa: F401  (ensure import path)
from gatekeep.providers.base import CompletionResult


class _FakeProvider:
    def __init__(self, texts):
        self._texts = list(texts)

    async def complete(self, payload):
        return CompletionResult(
            text=self._texts.pop(0), input_tokens=1, output_tokens=1, stop_reason="stop"
        )


def _gate_from(provider):
    from gatekeep.evals import make_eval_gate

    return make_eval_gate(
        provider=provider, generate_model="m", judge_model="claude-sonnet-5",
        max_tokens=64,
    )


async def test_promote_blocked_when_eval_gate_fails(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2 text", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="exact", expected="pong",
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
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains", expected="pong",
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompts.py -k gate -v`
Expected: FAIL with `TypeError: promote_prompt() got an unexpected keyword argument 'gate'`.

- [ ] **Step 3: Add the gate hook to promote_prompt**

In `gatekeep/prompts.py`, add a `TYPE_CHECKING`-only import of the `Gate` type alias (a runtime import would create a cycle once `evals.py` imports `prompts.py` in Task 5). Near the top imports:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gatekeep.evals import Gate
```

Change the signature (note the string annotation on `gate`, since `Gate` is only imported under `TYPE_CHECKING`) and insert the gate call after `target` is resolved but before any mutation:

```python
async def promote_prompt(
    name: str,
    version_num: int,
    session: AsyncSession,
    *,
    redis: Redis | None = None,
    gate: "Gate | None" = None,
) -> PromptVersion:
```

Immediately after the `if target is None: raise PromptVersionNotFoundError(...)` block, add:

```python
    if gate is not None:
        await gate(name, target, session)
```

Extend the docstring to note: when `gate` is provided it runs against the target version before the pointer flip; a raised `EvalGateFailure` leaves `active_version_id` and all caches untouched. Note `rollback_prompt` calls `promote_prompt` **without** a gate, so rollbacks are never eval-gated (reverting to an already-proven version).

> Import-cycle guard: `evals.py` will import `prompts.py` at runtime in Task 5, so `prompts.py` must import `Gate` only under `TYPE_CHECKING` (done above). Confirm after this step with `python -c "import gatekeep.prompts"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompts.py -v`
Expected: PASS (all existing tests still green + 3 new gate tests). Existing `promote_prompt` callers pass no `gate`, so they stay ungated.

- [ ] **Step 5: Wire the real gate + error surfacing into the CLI**

In `gatekeep/cli.py`, add imports:

```python
from anthropic import AsyncAnthropic

from gatekeep.evals import EvalGateFailure, make_eval_gate
from gatekeep.providers.anthropic import AnthropicProvider
```

Replace `_promote` with a gate-aware version:

```python
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
```

In `main()`, wrap the `prompt promote` dispatch so a gate failure prints the report and returns a distinct exit code. Add `EvalGateFailure` to the caught exceptions with a dedicated branch:

```python
    try:
        ...
    except EvalGateFailure as exc:
        run = exc.eval_run
        print(f"error: {exc}", file=sys.stderr)
        for item in run.report:
            status = "PASS" if item["passed"] else "FAIL"
            print(f"  [{status}] case {item['case_id']}: {item['reason']}", file=sys.stderr)
        return 2
    except (PromptNotFoundError, PromptVersionNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 6: Run the full prompt suite**

Run: `pytest tests/test_prompts.py tests/test_evals.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/prompts.py gatekeep/cli.py tests/test_prompts.py
git commit -m "feat(evals): gate prompt promotion behind eval suite; surface failures in CLI"
```

---

## Task 5: Eval management CLI (create-suite, add-case, run)

**Files:**
- Modify: `gatekeep/cli.py` (add the `eval` subparser with `create-suite`, `add-case`, `run`)
- Modify: `gatekeep/evals.py` (add `run_suite_for_prompt` convenience that resolves the active version)
- Test: extend `tests/test_evals.py`

**Interfaces:**
- Produces:
  - `async def run_suite_for_prompt(prompt_name, session, *, provider, generate_model, judge_model, max_tokens, version_num=None, include_unreviewed=False) -> EvalRun` - resolves the suite and the target `PromptVersion` (active version unless `version_num` given), then calls `run_eval_suite`.
  - CLI: `gatekeep eval create-suite <prompt_name> [--threshold F] [--name N]`, `gatekeep eval add-case <prompt_name> --input-file F.json --check-type {exact,contains,llm_judge} [--expected S] [--judge-criteria S]`, `gatekeep eval run <prompt_name> [--version N] [--model M] [--include-unreviewed]`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_evals.py`:

```python
from gatekeep.evals import run_suite_for_prompt
from gatekeep.prompts import add_prompt_version, create_prompt, promote_prompt


async def test_run_suite_for_prompt_uses_active_version_by_default(session):
    await create_prompt("system-context", "v1 template", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains", expected="ok",
    )
    provider = FakeProvider(["ok!"])

    run = await run_suite_for_prompt(
        "system-context", session,
        provider=provider, generate_model="m", judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    assert run.passed is True


async def test_run_suite_for_prompt_can_target_a_specific_version(session):
    await create_prompt("system-context", "v1", session)
    await add_prompt_version("system-context", "v2", session)
    suite = await create_suite("system-context", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "ping"}],
        check_type="contains", expected="ok",
    )
    provider = FakeProvider(["ok"])

    run = await run_suite_for_prompt(
        "system-context", session, version_num=2,
        provider=provider, generate_model="m", judge_model="claude-sonnet-5",
        max_tokens=64,
    )
    # the v2 version row was evaluated
    from gatekeep.models import PromptVersion
    v2 = (
        await session.execute(
            select(PromptVersion).where(PromptVersion.version_num == 2)
        )
    ).scalar_one()
    assert run.prompt_version_id == v2.id
```

(Add `from sqlalchemy import select` to the test file imports if not present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_evals.py -k run_suite_for_prompt -v`
Expected: FAIL with `ImportError: cannot import name 'run_suite_for_prompt'`.

- [ ] **Step 3: Implement run_suite_for_prompt**

In `gatekeep/evals.py`, add imports at the top:

```python
from gatekeep.prompts import (
    PromptNotFoundError,
    get_active_prompt_version,
    _get_prompt_row,
)
```

> This makes `evals.py` import `prompts.py` at runtime. That is safe because Task 4 already put `prompts.py`'s `Gate` import under `TYPE_CHECKING`, so `prompts.py` performs no runtime import of `evals.py`. Verify with `python -c "import gatekeep.cli"`.

Append:

```python
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
```

(The `prompts.py` `Gate` import is already `TYPE_CHECKING`-only from Task 4, so no change is needed there to avoid the cycle.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_evals.py -v && python -c "import gatekeep.cli"`
Expected: PASS and no import error.

- [ ] **Step 5: Add the `eval` CLI subcommands**

In `gatekeep/cli.py`, add imports:

```python
import json

from gatekeep.evals import add_case, create_suite, run_suite_for_prompt
```

Add async handlers:

```python
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
    name: str, input_file: str, check_type: str, expected: str | None, judge_criteria: str | None
) -> None:
    """Add a manual, reviewed case to a prompt's eval suite from a JSON messages file."""
    with open(input_file, encoding="utf-8") as f:
        input_messages = json.load(f)
    async with SessionLocal() as session:
        from gatekeep.evals import get_suite_for_prompt

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


async def _eval_run(name: str, version: int | None, model: str | None, include_unreviewed: bool) -> None:
    """Run a prompt's eval suite against a version/model and print the score."""
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
```

In `build_parser`, add the `eval` subparser after the `prompt` subparser:

```python
    eval_parser = subparsers.add_parser("eval", help="manage eval suites and cases")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)

    cs = eval_subparsers.add_parser("create-suite", help="create an eval suite for a prompt")
    cs.add_argument("name")
    cs.add_argument("--threshold", type=float, default=None)
    cs.add_argument("--name", dest="suite_name", default=None)

    ac = eval_subparsers.add_parser("add-case", help="add a manual case from a JSON messages file")
    ac.add_argument("name")
    ac.add_argument("--input-file", required=True)
    ac.add_argument("--check-type", choices=["exact", "contains", "llm_judge"], required=True)
    ac.add_argument("--expected", default=None)
    ac.add_argument("--judge-criteria", default=None)

    er = eval_subparsers.add_parser("run", help="run a prompt's eval suite")
    er.add_argument("name")
    er.add_argument("--version", type=int, default=None)
    er.add_argument("--model", default=None)
    er.add_argument("--include-unreviewed", action="store_true")
```

In `main()`, add an `elif args.command == "eval":` dispatch block mirroring the prompt one, routing to the three handlers.

- [ ] **Step 6: Smoke-test the parser**

Run: `python -c "from gatekeep.cli import build_parser; build_parser().parse_args(['eval','create-suite','p','--threshold','0.8'])"`
Expected: no error (namespace prints nothing).

- [ ] **Step 7: Commit**

```bash
git add gatekeep/cli.py gatekeep/evals.py gatekeep/prompts.py tests/test_evals.py
git commit -m "feat(evals): eval management CLI (create-suite, add-case, run)"
```

---

## Task 6: Curation pipeline

**Files:**
- Create: `gatekeep/curation.py`
- Modify: `gatekeep/cli.py` (add `eval curate`, `eval review`)
- Test: `tests/test_curation.py`

**Interfaces:**
- Consumes: `gatekeep.samples.recent_samples`, `gatekeep.evals.get_suite_for_prompt`, `add_case`, `EvalCase`.
- Produces:
  - `CURATED_JUDGE_CRITERIA = "output is a coherent, on-topic response to the input"`
  - `async def curate_cases(prompt_name, session, *, limit) -> list[EvalCase]` - reads recent samples, writes unreviewed `source="curated"`, `check_type="llm_judge"` cases.
  - `async def list_unreviewed(prompt_name, session) -> list[EvalCase]`
  - `async def review_case(case_id, session, *, approve: bool) -> None` - approve flips `reviewed=True`; reject deletes the row.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_curation.py`:

```python
import pytest
from sqlalchemy import select

from gatekeep.curation import (
    CURATED_JUDGE_CRITERIA,
    curate_cases,
    list_unreviewed,
    review_case,
)
from gatekeep.evals import create_suite
from gatekeep.models import ApiKey, EvalCase
from gatekeep.samples import record_request_sample


async def _seed_samples(session, prompt_name, n):
    key = ApiKey(name="k", key_hash="h")
    session.add(key)
    await session.flush()
    for i in range(n):
        await record_request_sample(
            session, key_id=key.id, prompt_name=prompt_name, model="m",
            input_messages=[{"role": "user", "content": f"q{i}"}], output_text=f"a{i}",
        )


async def test_curate_writes_unreviewed_llm_judge_cases(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 3)

    cases = await curate_cases("p", session, limit=2)
    assert len(cases) == 2
    for c in cases:
        assert c.reviewed is False
        assert c.source == "curated"
        assert c.check_type == "llm_judge"
        assert c.judge_criteria == CURATED_JUDGE_CRITERIA


async def test_curate_requires_a_suite(session):
    await _seed_samples(session, "p", 1)
    with pytest.raises(ValueError):
        await curate_cases("p", session, limit=1)


async def test_review_approve_marks_reviewed_and_reject_deletes(session):
    await create_suite("p", session, pass_threshold=0.9)
    await _seed_samples(session, "p", 2)
    cases = await curate_cases("p", session, limit=2)

    await review_case(cases[0].id, session, approve=True)
    await review_case(cases[1].id, session, approve=False)

    remaining = (await session.execute(select(EvalCase))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == cases[0].id
    assert remaining[0].reviewed is True

    assert await list_unreviewed("p", session) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_curation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.curation'`.

- [ ] **Step 3: Implement curation**

Create `gatekeep/curation.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.evals import add_case, get_suite_for_prompt
from gatekeep.models import EvalCase
from gatekeep.samples import recent_samples

CURATED_JUDGE_CRITERIA = "output is a coherent, on-topic response to the input"


async def curate_cases(
    prompt_name: str, session: AsyncSession, *, limit: int
) -> list[EvalCase]:
    """Mine the most recent request samples for a prompt into unreviewed eval cases.

    Each sample becomes an unreviewed `source="curated"`, `check_type="llm_judge"`
    case with a generic judge criteria as a starting point for human tightening.
    Raises ValueError if no eval suite is registered for the prompt.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        raise ValueError(f"no eval suite registered for prompt {prompt_name!r}")

    samples = await recent_samples(prompt_name, session, limit=limit)
    cases: list[EvalCase] = []
    for sample in samples:
        case = await add_case(
            suite.id,
            session,
            input_messages=sample.input_messages,
            check_type="llm_judge",
            judge_criteria=CURATED_JUDGE_CRITERIA,
            reviewed=False,
            source="curated",
        )
        cases.append(case)
    return cases


async def list_unreviewed(
    prompt_name: str, session: AsyncSession
) -> list[EvalCase]:
    """List unreviewed curated cases for a prompt, oldest first."""
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        return []
    result = await session.execute(
        select(EvalCase)
        .where(EvalCase.suite_id == suite.id, EvalCase.reviewed.is_(False))
        .order_by(EvalCase.id)
    )
    return list(result.scalars().all())


async def review_case(
    case_id: int, session: AsyncSession, *, approve: bool
) -> None:
    """Approve (flip reviewed=True) or reject (delete) one curated case.

    Raises ValueError if the case id does not exist.
    """
    case = await session.get(EvalCase, case_id)
    if case is None:
        raise ValueError(f"no eval case with id {case_id}")
    if approve:
        case.reviewed = True
    else:
        await session.delete(case)
    await session.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_curation.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Add curate/review CLI**

In `gatekeep/cli.py`, add imports:

```python
from gatekeep.curation import curate_cases, list_unreviewed, review_case
```

Add handlers:

```python
async def _eval_curate(name: str, limit: int) -> None:
    """Mine recent request samples for a prompt into unreviewed curated cases."""
    async with SessionLocal() as session:
        cases = await curate_cases(name, session, limit=limit)
    print(f"curated {len(cases)} unreviewed case(s) for {name!r}; review them before they gate")


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
```

Add subparsers under `eval_subparsers`:

```python
    cu = eval_subparsers.add_parser("curate", help="mine request samples into unreviewed cases")
    cu.add_argument("name")
    cu.add_argument("--limit", type=int, default=10)

    rv = eval_subparsers.add_parser("review", help="approve/reject unreviewed curated cases")
    rv.add_argument("name")
```

Route both in the `eval` dispatch block in `main()`.

- [ ] **Step 6: Smoke-test + full suite**

Run: `pytest tests/test_curation.py tests/test_samples.py -v && python -c "from gatekeep.cli import build_parser; build_parser().parse_args(['eval','curate','p','--limit','5'])"`
Expected: PASS + no parser error.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/curation.py gatekeep/cli.py tests/test_curation.py
git commit -m "feat(evals): curation pipeline (mine samples -> unreviewed cases -> human review)"
```

---

## Task 7: In-repo prompt files and sync (decided Q1 = Option B)

**Files:**
- Create: `prompts/` directory with `prompts/README.md` and one seed template `prompts/system-context.txt`
- Modify: `gatekeep/prompts.py` (add `sync_prompt_from_text`)
- Modify: `gatekeep/cli.py` (add `prompt sync <dir>`)
- Test: `tests/test_prompt_sync.py`

**Interfaces:**
- Produces:
  - `async def sync_prompt_from_text(name, template, session) -> PromptVersion` - if the prompt does not exist, create it (version 1, active); if it exists and the text differs from the active version, add a new inactive version; if the text matches the active version, no-op. Returns the active-or-new version.
  - CLI: `gatekeep prompt sync <dir>` - for each `*.txt` in `<dir>`, sync using the filename stem as the prompt name; prints what changed.

> Rationale: the file is the reviewed source of truth (PR diff + CI gate happen on it). Sync is idempotent so a merge job can run it repeatedly. It only ever **adds** versions - promotion stays an explicit, eval-gated step so merging a file never silently activates an unproven template.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_prompt_sync.py`:

```python
from gatekeep.prompts import (
    add_prompt_version,
    create_prompt,
    get_active_prompt_version,
    sync_prompt_from_text,
)


async def test_sync_creates_prompt_when_absent(session):
    version = await sync_prompt_from_text("system-context", "hello", session)
    assert version.version_num == 1
    active = await get_active_prompt_version("system-context", session)
    assert active.template == "hello"


async def test_sync_adds_inactive_version_when_text_changes(session):
    await create_prompt("system-context", "v1", session)
    version = await sync_prompt_from_text("system-context", "v2 text", session)
    assert version.version_num == 2
    assert version.active is False
    # active is still v1 until an explicit, gated promote
    active = await get_active_prompt_version("system-context", session)
    assert active.version_num == 1


async def test_sync_is_noop_when_text_matches_active(session):
    await create_prompt("system-context", "same", session)
    version = await sync_prompt_from_text("system-context", "same", session)
    assert version.version_num == 1  # no new version created
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt_sync.py -v`
Expected: FAIL with `ImportError: cannot import name 'sync_prompt_from_text'`.

- [ ] **Step 3: Implement sync_prompt_from_text**

In `gatekeep/prompts.py`, append:

```python
async def sync_prompt_from_text(
    name: str, template: str, session: AsyncSession
) -> PromptVersion:
    """Idempotently reconcile a prompt with in-repo template text.

    Creates the prompt (version 1, active) if it does not exist; adds a new
    inactive version if the text differs from the current active version;
    returns the existing active version unchanged if the text already matches.
    Never promotes - activation stays an explicit, eval-gated step.
    """
    existing = (
        await session.execute(select(Prompt).where(Prompt.name == name))
    ).scalar_one_or_none()
    if existing is None:
        prompt = await create_prompt(name, template, session)
        return await get_active_prompt_version(prompt.name, session)

    active = await get_active_prompt_version(name, session)
    if active.template == template:
        return active
    return await add_prompt_version(name, template, session)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt_sync.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Create the prompts directory + seed file**

Create `prompts/system-context.txt`:

```
You are a helpful assistant. Answer clearly and concisely.
```

Create `prompts/README.md`:

```markdown
# Prompt templates

Each `*.txt` file here is the reviewed source of truth for a prompt, named by
its filename stem (`system-context.txt` -> prompt `system-context`). Changes go
through a PR so the diff gets human review and the eval-gate CI check
(`.github/workflows/eval-gate.yml`) runs against the change.

Merging a file **adds** a version via `gatekeep prompt sync prompts/`; it never
activates it. Promote explicitly with `gatekeep prompt promote <name> <version>`,
which runs the eval gate.
```

- [ ] **Step 6: Add the `prompt sync` CLI command**

In `gatekeep/cli.py`, add a handler:

```python
async def _sync(directory: str) -> None:
    """Sync every prompts/*.txt file into the DB, adding versions where text changed."""
    import pathlib

    paths = sorted(pathlib.Path(directory).glob("*.txt"))
    async with SessionLocal() as session:
        for path in paths:
            template = path.read_text(encoding="utf-8")
            name = path.stem
            version = await sync_prompt_from_text(name, template, session)
            print(f"{name}\tv{version.version_num}{' (active)' if version.active else ' (new, not active)'}")
```

Add `sync_prompt_from_text` to the `gatekeep.prompts` import block, register a `sync` subparser under `prompt_subparsers` with one `directory` argument, and route it in `main()`.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/prompts.py gatekeep/cli.py prompts/ tests/test_prompt_sync.py
git commit -m "feat(prompts): in-repo prompt files with idempotent sync command"
```

---

## Task 8: GitHub Actions eval-gate CI

**Files:**
- Create: `scripts/ci-eval-check.sh`
- Create: `.github/workflows/eval-gate.yml`

> No unit test - this is CI glue. It is validated by the workflow running on a PR. `ci-eval-check.sh` reuses `gatekeep eval run` against a throwaway version synced from the changed file.

- [ ] **Step 1: Write the CI check script**

Create `scripts/ci-eval-check.sh`:

```bash
#!/usr/bin/env bash
# Sync in-repo prompt files into a fresh DB and run each suite against the
# newly-synced (not-yet-promoted) version. Exit non-zero if any suite fails,
# turning the PR check red. Prompts with no registered suite are skipped by
# `gatekeep eval run` raising a "no suite" error, which we treat as a skip.
set -euo pipefail

echo "Applying migrations..."
alembic upgrade head

echo "Syncing prompt files..."
gatekeep prompt sync prompts/

status=0
for file in prompts/*.txt; do
  [ -e "$file" ] || continue
  name="$(basename "$file" .txt)"
  echo "== eval: $name =="
  if output="$(gatekeep eval run "$name" 2>&1)"; then
    code=0
  else
    code=$?
  fi
  echo "$output"
  if [ "$code" -ne 0 ]; then
    # Distinguish "no suite" (skip) from a real gate failure, without
    # re-running the command (its provider calls are not free/idempotent-cheap).
    if echo "$output" | grep -q "no eval suite registered"; then
      echo "  (no suite; skipping)"
    else
      echo "  FAILED"
      status=1
    fi
  fi
done
exit "$status"
```

Make it executable: `chmod +x scripts/ci-eval-check.sh`.

- [ ] **Step 2: Write the workflow**

Create `.github/workflows/eval-gate.yml`:

```yaml
name: eval-gate

on:
  pull_request:
    paths:
      - "prompts/**"

jobs:
  eval-gate:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: gatekeep
          POSTGRES_PASSWORD: gatekeep
          POSTGRES_DB: gatekeep
        ports:
          - "5432:5432"
        options: >-
          --health-cmd "pg_isready -U gatekeep"
          --health-interval 5s
          --health-timeout 3s
          --health-retries 10
      redis:
        image: redis:7
        ports:
          - "6379:6379"
    env:
      DATABASE_URL: postgresql+asyncpg://gatekeep:gatekeep@localhost:5432/gatekeep
      REDIS_URL: redis://localhost:6379/0
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: pip install -e ".[dev]"
      - name: Run eval gate
        run: bash scripts/ci-eval-check.sh
```

> Requires an `ANTHROPIC_API_KEY` repo secret (the `llm_judge` cases and generation call the real provider). Document this in the README (Task 10).

- [ ] **Step 3: Lint the shell script**

Run: `bash -n scripts/ci-eval-check.sh`
Expected: no syntax errors.

- [ ] **Step 4: Commit**

```bash
git add scripts/ci-eval-check.sh .github/workflows/eval-gate.yml
git commit -m "ci(evals): run eval gate on prompt-template PRs"
```

---

## Task 9: Cost-based routing (opt-in)

**Files:**
- Create: `gatekeep/routing.py`
- Modify: `gatekeep/api/openai_schemas.py` (add `route_by_cost`, `quality_floor` to the request)
- Modify: `gatekeep/app.py` (optional routing step + log `routed_from`)
- Test: `tests/test_routing.py`

> This is the most speculative task (per the spec). Routing only ever substitutes a **cheaper** model that has a **passing** recent `EvalRun` at or above `quality_floor` for the request's prompt suite; otherwise it returns the requested model unchanged. It never overrides an explicit model unless `route_by_cost` is set.

**Interfaces:**
- Consumes: `gatekeep.accounting.MODEL_PRICING`, `EvalRun`, `EvalSuite`.
- Produces: `async def select_model(requested_model, prompt_name, quality_floor, session) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_routing.py`:

```python
from gatekeep.evals import create_suite
from gatekeep.models import EvalRun, Prompt, PromptVersion
from gatekeep.routing import select_model


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
            suite_id=suite_id, prompt_version_id=version_id, model=model,
            score=score, passed=passed, report=[],
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_routing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.routing'`.

- [ ] **Step 3: Implement select_model**

Create `gatekeep/routing.py`:

```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.accounting import MODEL_PRICING
from gatekeep.evals import get_suite_for_prompt
from gatekeep.models import EvalRun


def _model_cost(model: str) -> float:
    """Return a single comparable cost figure (input + output per-1M price) for a model."""
    input_price, output_price = MODEL_PRICING.get(model, (0.0, 0.0))
    return input_price + output_price


async def select_model(
    requested_model: str,
    prompt_name: str,
    quality_floor: float,
    session: AsyncSession,
) -> str:
    """Pick the cheapest model that clears `quality_floor` for this prompt's suite.

    Considers only models strictly cheaper than `requested_model` that have a
    most-recent EvalRun with `passed` True and `score >= quality_floor` for the
    prompt's suite. Returns `requested_model` unchanged when no suite exists or
    no cheaper qualifying model is found. Never returns a more expensive model.
    """
    suite = await get_suite_for_prompt(prompt_name, session)
    if suite is None:
        return requested_model

    requested_cost = _model_cost(requested_model)
    candidates = [
        model
        for model in MODEL_PRICING
        if _model_cost(model) < requested_cost
    ]
    if not candidates:
        return requested_model

    best_model = requested_model
    best_cost = requested_cost
    for model in candidates:
        latest = (
            await session.execute(
                select(EvalRun)
                .where(EvalRun.suite_id == suite.id, EvalRun.model == model)
                .order_by(EvalRun.created_at.desc(), EvalRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None or not latest.passed or latest.score < quality_floor:
            continue
        if _model_cost(model) < best_cost:
            best_model = model
            best_cost = _model_cost(model)
    return best_model
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routing.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Add opt-in request fields + wire into app.py**

In `gatekeep/api/openai_schemas.py`, add to `ChatCompletionRequest` (alongside the existing `prompt_name` extension field):

```python
    route_by_cost: bool = False
    quality_floor: float | None = None
```

In `gatekeep/app.py`, add the import:

```python
from gatekeep.routing import select_model
```

In `chat_completions`, after `model = payload["model"]` and before `requests_total.labels(...)`, insert:

```python
    routed_from = None
    if req.route_by_cost and req.prompt_name is not None:
        floor = req.quality_floor if req.quality_floor is not None else 0.0
        chosen = await select_model(model, req.prompt_name, floor, session)
        if chosen != model:
            routed_from = model
            model = chosen
            payload["model"] = chosen
```

Thread `routed_from=routed_from` into the non-streaming provider-served `log_request(...)` call (the same one that now passes `prompt_name`). Update the endpoint docstring to describe the opt-in routing step.

> Streaming path: leave routing out of the SSE branch for Phase 3 (routing reads eval history that only the non-streaming path populates samples for). Note this in the docstring.

- [ ] **Step 6: Run routing + endpoint tests**

Run: `pytest tests/test_routing.py tests/test_endpoint.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/routing.py gatekeep/api/openai_schemas.py gatekeep/app.py tests/test_routing.py
git commit -m "feat(routing): opt-in cost-based model selection gated on eval quality floor"
```

---

## Task 10: Documentation

**Files:**
- Modify: `README.md`
- Modify: `.env.example` (document the two new settings)

- [ ] **Step 1: Document the new settings in .env.example**

Append to `.env.example`:

```
# Eval gate (Phase 3)
# Fixed, stronger judge model used for llm_judge eval checks (decoupled from the
# model under test to avoid self-preference bias).
EVAL_JUDGE_MODEL=claude-sonnet-5
# Default pass_threshold applied to a new suite when none is given.
EVAL_PASS_THRESHOLD_DEFAULT=0.9
```

- [ ] **Step 2: Add an eval-gate section to the README**

Add a new section after the prompt-management docs:

````markdown
## Eval gate and prompt quality control

Prompt templates live in-repo under `prompts/` (one `*.txt` per prompt, named by
filename). A change is a PR: the diff gets reviewed and the `eval-gate` workflow
runs the prompt's eval suite against the change.

Register a suite and cases, then curate more from real traffic:

```bash
# One suite per prompt; threshold defaults to EVAL_PASS_THRESHOLD_DEFAULT.
gatekeep eval create-suite system-context --threshold 0.9

# Add a deterministic case from a JSON messages file.
echo '[{"role":"user","content":"ping"}]' > /tmp/case.json
gatekeep eval add-case system-context --input-file /tmp/case.json \
  --check-type contains --expected pong

# Grow the suite from recent real traffic (writes unreviewed cases).
gatekeep eval curate system-context --limit 20
gatekeep eval review system-context   # approve/reject each, interactively

# Run the suite manually (defaults to the active version + DEFAULT_MODEL).
gatekeep eval run system-context
```

Promotion is gated: `gatekeep prompt promote <name> <version>` runs the suite
first and refuses to activate a version that scores below the suite threshold,
printing a per-case report. Prompts with no suite promote exactly as before
(the gate is opt-in). `rollback` is never gated.

The `llm_judge` check grades output with a fixed, stronger judge model
(`EVAL_JUDGE_MODEL`, default `claude-sonnet-5`) rather than the model under
test, to avoid a model rubber-stamping its own failure mode.

CI requires an `ANTHROPIC_API_KEY` repository secret so the gate's generation
and judge calls can reach the provider.

### Cost-based routing (opt-in)

Send `"route_by_cost": true` (optionally with `"quality_floor": 0.9`) alongside
`"prompt_name"` to let the gateway substitute the cheapest model that has a
passing eval run at or above the floor for that prompt. It never overrides an
explicit model choice unless you opt in, and never routes up to a costlier
model. The substitution is recorded in `request_logs.routed_from`.
````

- [ ] **Step 3: Commit**

```bash
git add README.md .env.example
git commit -m "docs(evals): document eval gate, curation, and cost routing"
```

---

## Task 11: In-repo eval-case fixtures for CI (closes CI no-op gap)

**Why this task exists:** The final whole-branch review of Tasks 1-10 found that `.github/workflows/eval-gate.yml` spins up a fresh, empty ephemeral Postgres for every run, and nothing seeds `EvalSuite`/`EvalCase` rows into it. `gatekeep eval run <name>` therefore always raises "no eval suite registered", which `ci-eval-check.sh` treats as a skip — so the CI gate can never fail on a real regression, contradicting the Definition of Done ("GitHub Actions workflow ... fails the check on eval regression"). This was a gap in the plan itself (Task 5/8 never specified how CI's DB gets suites/cases), not a defect in what was built. Resolution (decided): in-repo case fixtures, one JSON file per prompt, committed alongside `prompts/*.txt`, loaded into CI's ephemeral DB before the eval run — consistent with the project's Q1 decision that prompt review happens in-repo via PR diff.

**Files:**
- Create: `gatekeep/fixtures.py` — fixture-file loading (`load_fixture_file`, `load_fixtures_dir`)
- Modify: `gatekeep/cli.py` — add `gatekeep eval load-fixtures <dir>`
- Modify: `scripts/ci-eval-check.sh` — call `gatekeep eval load-fixtures prompts/` before the eval-run loop
- Create: `prompts/system-context.cases.json` — seed fixture so the CI gate has something real to run against
- Test: `tests/test_fixtures.py`

**Fixture file format** (`prompts/<name>.cases.json`, sibling of `prompts/<name>.txt`):

```json
{
  "pass_threshold": 1.0,
  "cases": [
    {
      "input_messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
      "check_type": "contains",
      "expected": "4"
    }
  ]
}
```

- `pass_threshold`: float, required.
- `cases`: list of objects, each with `input_messages` (list of `{role, content}` dicts, required), `check_type` (`"exact"` | `"contains"` | `"llm_judge"`, required), and `expected` (required for exact/contains) or `judge_criteria` (required for llm_judge) — same validation `add_case` already performs.

**Behavior:**
- Fixtures are idempotent and safe to load repeatedly (CI's DB is fresh every run, but the same loader must also not corrupt a persistent dev DB if run there): loading a fixture for `<name>` gets-or-creates the `EvalSuite` for that prompt (creating it if absent, otherwise **updating its `pass_threshold`** to match the fixture — the fixture is the source of truth for CI-gating cases), then **deletes only the suite's existing `source="fixture"` cases** before inserting the fixture's cases fresh with `source="fixture"`, `reviewed=True`. It never touches `source="manual"` or `source="curated"` cases, so fixture loading cannot destroy hand-added or curated-and-approved cases in a suite that also has fixture cases.
- `EvalCase.source` gains a third value, `"fixture"`, alongside the existing `"manual"`/`"curated"` — this is just a string column (no CHECK constraint in migration `0007`), so no new migration is needed.

**Interfaces:**
- Consumes: `gatekeep.evals.get_suite_for_prompt`, `create_suite`, `add_case`; `gatekeep.models.EvalCase`.
- Produces:
  - `async def load_fixture_file(path: pathlib.Path, session: AsyncSession) -> EvalSuite`
  - `async def load_fixtures_dir(directory: str, session: AsyncSession) -> list[EvalSuite]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fixtures.py`:

```python
import json

import pytest
from sqlalchemy import select

from gatekeep.evals import create_suite, get_suite_for_prompt
from gatekeep.fixtures import load_fixture_file, load_fixtures_dir
from gatekeep.models import EvalCase


def _write_fixture(tmp_path, name, pass_threshold, cases):
    path = tmp_path / f"{name}.cases.json"
    path.write_text(json.dumps({"pass_threshold": pass_threshold, "cases": cases}))
    return path


async def test_load_fixture_file_creates_suite_and_cases(tmp_path, session):
    path = _write_fixture(
        tmp_path,
        "system-context",
        1.0,
        [
            {
                "input_messages": [{"role": "user", "content": "2+2?"}],
                "check_type": "contains",
                "expected": "4",
            }
        ],
    )

    suite = await load_fixture_file(path, session)
    assert suite.prompt_name == "system-context"
    assert suite.pass_threshold == 1.0

    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    assert len(cases) == 1
    assert cases[0].source == "fixture"
    assert cases[0].reviewed is True
    assert cases[0].check_type == "contains"


async def test_load_fixture_file_updates_threshold_and_replaces_fixture_cases(
    tmp_path, session
):
    await create_suite("system-context", session, pass_threshold=0.5)
    path = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "hi"}],
                "check_type": "contains",
                "expected": "hello",
            }
        ],
    )

    await load_fixture_file(path, session)
    # loading again with different content must not duplicate rows
    path2 = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "bye"}],
                "check_type": "contains",
                "expected": "goodbye",
            }
        ],
    )
    suite = await load_fixture_file(path2, session)

    assert suite.pass_threshold == 0.9
    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    assert len(cases) == 1
    assert cases[0].expected == "goodbye"


async def test_load_fixture_file_never_touches_manual_or_curated_cases(
    tmp_path, session
):
    from gatekeep.evals import add_case

    suite = await create_suite("system-context", session, pass_threshold=0.9)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "manual"}],
        check_type="contains",
        expected="m",
        source="manual",
    )
    path = _write_fixture(
        tmp_path,
        "system-context",
        0.9,
        [
            {
                "input_messages": [{"role": "user", "content": "fixture"}],
                "check_type": "contains",
                "expected": "f",
            }
        ],
    )

    await load_fixture_file(path, session)

    cases = (
        (await session.execute(select(EvalCase).where(EvalCase.suite_id == suite.id)))
        .scalars()
        .all()
    )
    sources = {c.source for c in cases}
    assert sources == {"manual", "fixture"}
    assert len(cases) == 2


async def test_load_fixtures_dir_loads_every_cases_json(tmp_path, session):
    _write_fixture(tmp_path, "a", 1.0, [])
    _write_fixture(tmp_path, "b", 1.0, [])

    suites = await load_fixtures_dir(str(tmp_path), session)
    names = {s.prompt_name for s in suites}
    assert names == {"a", "b"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fixtures.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.fixtures'`.

- [ ] **Step 3: Implement gatekeep/fixtures.py**

```python
from __future__ import annotations

import json
import pathlib

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.evals import add_case, create_suite, get_suite_for_prompt
from gatekeep.models import EvalCase, EvalSuite


async def load_fixture_file(path: pathlib.Path, session: AsyncSession) -> EvalSuite:
    """Load one `<name>.cases.json` fixture into the DB, idempotently.

    Gets-or-creates the EvalSuite for the fixture's prompt name (the
    filename stem), updating pass_threshold to match the fixture. Deletes
    only this suite's existing source="fixture" cases before inserting the
    fixture's cases fresh with source="fixture", reviewed=True - manual and
    curated cases on the same suite are never touched, so re-running this
    (e.g. in CI, or against a persistent dev DB) is safe and repeatable.
    """
    name = path.stem.removesuffix(".cases")
    data = json.loads(path.read_text(encoding="utf-8"))
    pass_threshold = data["pass_threshold"]
    cases = data.get("cases", [])

    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        suite = await create_suite(name, session, pass_threshold=pass_threshold)
    else:
        suite.pass_threshold = pass_threshold
        await session.commit()
        await session.refresh(suite)

    await session.execute(
        delete(EvalCase).where(
            EvalCase.suite_id == suite.id, EvalCase.source == "fixture"
        )
    )
    await session.commit()

    for case in cases:
        await add_case(
            suite.id,
            session,
            input_messages=case["input_messages"],
            check_type=case["check_type"],
            expected=case.get("expected"),
            judge_criteria=case.get("judge_criteria"),
            reviewed=True,
            source="fixture",
        )

    return suite


async def load_fixtures_dir(directory: str, session: AsyncSession) -> list[EvalSuite]:
    """Load every `*.cases.json` fixture file in `directory`."""
    suites = []
    for path in sorted(pathlib.Path(directory).glob("*.cases.json")):
        suites.append(await load_fixture_file(path, session))
    return suites
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_fixtures.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Add the `gatekeep eval load-fixtures` CLI command**

In `gatekeep/cli.py`, add `load_fixtures_dir` to the top-level `from gatekeep.fixtures import load_fixtures_dir` import (new top-level import line, not local), add a handler:

```python
async def _eval_load_fixtures(directory: str) -> None:
    """Load every prompts/*.cases.json fixture into the DB (idempotent)."""
    async with SessionLocal() as session:
        suites = await load_fixtures_dir(directory, session)
    for suite in suites:
        print(f"loaded fixture cases for {suite.prompt_name!r} (threshold {suite.pass_threshold})")
```

Add a subparser under `eval_subparsers`:

```python
    lf = eval_subparsers.add_parser(
        "load-fixtures", help="load prompts/*.cases.json fixtures into the DB"
    )
    lf.add_argument("directory")
```

Route it in the `eval` dispatch block in `main()`.

- [ ] **Step 6: Wire the loader into the CI script**

In `scripts/ci-eval-check.sh`, add a step between "Syncing prompt files..." and the eval-run loop:

```bash
echo "Loading eval-case fixtures..."
gatekeep eval load-fixtures prompts/
```

- [ ] **Step 7: Add the seed fixture**

Create `prompts/system-context.cases.json`:

```json
{
  "pass_threshold": 1.0,
  "cases": [
    {
      "input_messages": [{"role": "user", "content": "What is 2+2? Answer with just the number."}],
      "check_type": "contains",
      "expected": "4"
    }
  ]
}
```

- [ ] **Step 8: Update prompts/README.md**

Add a short paragraph documenting the new fixture file convention: `<name>.cases.json` sits alongside `<name>.txt`, holds the CI gate's eval suite/cases for that prompt, and is loaded via `gatekeep eval load-fixtures prompts/` (which CI already runs). Note it only manages `source="fixture"` cases and never touches manually-added or curated-and-approved ones.

- [ ] **Step 9: Verify end to end**

Run:
```bash
alembic upgrade head
gatekeep prompt sync prompts/
gatekeep eval load-fixtures prompts/
gatekeep eval run system-context
```
Expected: `gatekeep eval run system-context` prints `[PASS]` (requires a real `ANTHROPIC_API_KEY` in `.env` to actually call the provider - if unavailable, at minimum confirm `gatekeep eval load-fixtures prompts/` prints `loaded fixture cases for 'system-context' (threshold 1.0)` and that a suite/case now exists in the DB, proving the CI no-op gap is closed).

- [ ] **Step 10: Full suite + lint**

Run: `pytest -q && ruff check gatekeep tests && ruff format --check gatekeep tests`
Expected: all green, no lint/format issues on the new/modified files.

- [ ] **Step 11: Commit**

```bash
git add gatekeep/fixtures.py gatekeep/cli.py scripts/ci-eval-check.sh prompts/system-context.cases.json prompts/README.md tests/test_fixtures.py
git commit -m "feat(evals): in-repo eval-case fixtures loaded into CI's ephemeral DB, closing the no-op gate gap"
```

---

## Final verification

- [ ] **Run the full test suite**

Run: `pytest -v`
Expected: all green, including `test_eval_models`, `test_samples`, `test_evals`, `test_curation`, `test_routing`, `test_prompt_sync`, and the extended `test_prompts`.

- [ ] **Lint and format**

Run: `ruff check gatekeep tests && ruff format --check gatekeep tests`
Expected: no issues.

- [ ] **Migration round-trips on a clean DB**

Run: `alembic upgrade head && alembic downgrade 0006 && alembic upgrade head`
Expected: no errors.

---

## Definition of Done (from the scope doc)

- [ ] `pytest -v` fully green including new eval tests
- [ ] `gatekeep prompt promote` blocks on a failing eval suite and prints a report; ungated when no suite is registered
- [ ] `gatekeep eval curate` pulls real traffic (`request_samples`) into unreviewed `EvalCase` rows; `gatekeep eval review` lets a human approve/reject
- [ ] GitHub Actions workflow runs on prompt-template PRs and fails on eval regression (requires Task 11's in-repo case fixtures — CI's ephemeral DB has no suites/cases without them)
- [ ] Cost-based routing implemented behind an opt-in flag, never silently overriding an explicit model request
- [ ] README updated with eval gate + curation workflow examples
