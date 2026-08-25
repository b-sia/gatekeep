# Prompt Operations Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated **Prompts** tab to the operator dashboard that surfaces every prompt-lifecycle operation (version create/promote/rollback, A/B candidate config, eval-suite management, curation review) through operator-gated HTTP mutation endpoints, backed by an append-only audit log and an in-process Redis-backed background-job channel for long-running eval/promote operations.

**Architecture:** The pure service layer (`prompts.py`, `evals.py`, `curation.py`) stays unchanged. New HTTP mutation endpoints in `gatekeep/api/dashboard.py` call those services, then record an audit event via a new `gatekeep/audit.py` helper. Long-running operations (eval-run, promote) return a job id immediately and run in an `asyncio.create_task` managed by a new `gatekeep/promptjobs.py`, writing status to Redis and their audit event on completion. The React dashboard gains a `PromptsPage` (master-detail) and a `useJob` polling hook.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, Pydantic, Redis (`redis.asyncio`); React + TypeScript + Vite + Tailwind, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-23-prompt-operations-dashboard-design.md`

## Global Constraints

- **No em dashes** anywhere (code, comments, copy, commit messages). Use a plain `-`.
- **Docstrings required** on every new Python function/method/class (purpose, params, returns, raises). TSDoc/JSDoc on every new TS function/component.
- **Service layer stays pure and unchanged:** do NOT add audit writes, HTTP concerns, or job logic inside `prompts.py` / `evals.py` / `curation.py`. Audit writes live in the endpoint/job layer only.
- **All new endpoints** live on the existing `router` in `gatekeep/api/dashboard.py` (prefix `/dashboard/api`) and are gated with `Depends(require_operator)`.
- **Error envelope:** all HTTP errors use the OpenAI-shaped body via the existing `_error_body(...)` / `_forbidden(...)` helpers. Map service exceptions: `PromptNotFoundError` / `PromptVersionNotFoundError` -> 404; duplicate-name and invalid `traffic_pct` `ValueError` -> 400; `EvalGateFailure` is NOT an HTTP error (it becomes a job `blocked` result).
- **Actor attribution:** every mutation sets `actor_account_id = caller.id`, `actor_label = caller.name`, and passes `created_by=caller.name` to services that accept it (`create_prompt`, `add_prompt_version`).
- **Audit results are first-class:** record `result="success"`, `result="blocked"` (eval gate stopped a promote), or `result="error"` (operation raised). The two async operations write their audit event from the background task so the terminal outcome is captured accurately.
- **Migration numbering:** next Alembic revision is `0024`, `down_revision = "0023"`.
- **Ruff** must pass (`ruff check` + `ruff format`); it runs on commit via pre-commit. **Prettier/tsc/eslint** conventions match the existing dashboard.
- **Tests use the real ASGI app** (`from gatekeep.app import app`) and a real Postgres test DB + real Redis, per `tests/conftest.py`. Inject fake providers into async jobs via `app.dependency_overrides`.

---

## File Structure

**Backend (new):**
- `gatekeep/audit.py` - `record_audit_event(...)` helper; the only writer of `audit_event` rows.
- `gatekeep/promptjobs.py` - Redis-backed job channel: job dataclass, create/get/update, the `run_eval_job` / `run_promote_job` coroutines, and the task registry.
- `migrations/versions/0024_audit_event.py` - the `audit_event` table.

**Backend (modified):**
- `gatekeep/models.py` - add `AuditEvent` ORM model.
- `gatekeep/api/dashboard.py` - new request/response models, read extensions, and all new mutation + job-poll endpoints.

**Frontend (new):**
- `dashboard/src/pages/PromptsPage.tsx` - master-detail Prompts tab.
- `dashboard/src/components/PromptDetail.tsx` - right pane container for the selected prompt.
- `dashboard/src/components/prompts/VersionsSection.tsx`
- `dashboard/src/components/prompts/CandidateSection.tsx`
- `dashboard/src/components/prompts/EvalsSection.tsx`
- `dashboard/src/components/prompts/CurationSection.tsx`
- `dashboard/src/components/prompts/AuditFeed.tsx`
- `dashboard/src/hooks/useJob.ts` + `dashboard/src/hooks/useJob.test.ts`

**Frontend (modified):**
- `dashboard/src/api/types.ts`, `dashboard/src/api/client.ts` (+ `client.test.ts`).
- `dashboard/src/components/Header.tsx` - add `"prompts"` tab.
- `dashboard/src/App.tsx` - route the new tab.
- `dashboard/src/pages/DashboardPage.tsx` - remove the moved `PromptsPanel`/`EvalHistoryPanel`.

---

## Task 1: `audit_event` table (model + migration)

**Files:**
- Modify: `gatekeep/models.py` (append `AuditEvent` after `EvalRun`, ~line 385)
- Create: `migrations/versions/0024_audit_event.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces: `AuditEvent` ORM model with columns `id, created_at, actor_account_id, actor_label, action, entity_type, entity_ref, version_num, result, details`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_audit.py`:

```python
from __future__ import annotations

from gatekeep.models import AuditEvent


async def test_audit_event_row_roundtrips(session):
    event = AuditEvent(
        actor_account_id=None,
        actor_label="op-acct",
        action="prompt.promote",
        entity_type="prompt",
        entity_ref="greeting",
        version_num=2,
        result="success",
        details={"from_version": 1, "to_version": 2},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)

    assert event.id is not None
    assert event.created_at is not None
    assert event.details["to_version"] == 2
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_audit.py::test_audit_event_row_roundtrips -v`
Expected: FAIL with `ImportError: cannot import name 'AuditEvent'`.

- [ ] **Step 3: Add the `AuditEvent` model**

In `gatekeep/models.py`, append after the `EvalRun` class:

```python
class AuditEvent(Base):
    """One append-only record of a mutating operator action.

    Fleet-wide by design: `actor_account_id` is nullable so events survive
    the actor account being deleted, with `actor_label` holding the
    operator's name denormalized at action time so the log stays readable.
    `result` is one of `success` / `blocked` / `error`, so a promotion
    stopped by the eval gate and a failed eval run are recorded as
    first-class outcomes, not just successes. `details` carries
    action-specific JSON (from/to version, eval score, traffic pct, case
    count, error message, etc.). Prompt/eval operations are the first
    producers; account/key producers can be added later with no schema
    change.
    """

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    actor_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    actor_label: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index(
            "ix_audit_events_entity",
            "entity_type",
            "entity_ref",
            "created_at",
        ),
        Index("ix_audit_events_action_created_at", "action", "created_at"),
    )
```

Note: `Index`, `JSONB`, `ForeignKey`, `String`, `Integer`, `DateTime`, `Mapped`, `mapped_column`, `_utcnow` are all already imported/defined in `models.py`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_audit.py::test_audit_event_row_roundtrips -v`
Expected: PASS (the autouse `_create_schema` fixture creates the table from metadata).

- [ ] **Step 5: Write the Alembic migration**

Create `migrations/versions/0024_audit_event.py`:

```python
"""audit_events append-only fleet-wide audit log

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-24

Adds the generic audit_events table. Prompt and eval mutations are its
first producers; account/key producers can be added later with no schema
change. Append-only: no update/delete paths in the application.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_account_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_ref", sa.String(length=255), nullable=True),
        sa.Column("version_num", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("details", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_foreign_key(
        "fk_audit_events_actor_account_id",
        "audit_events",
        "accounts",
        ["actor_account_id"],
        ["id"],
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_ref", "created_at"],
    )
    op.create_index(
        "ix_audit_events_action_created_at",
        "audit_events",
        ["action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_action_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_constraint(
        "fk_audit_events_actor_account_id", "audit_events", type_="foreignkey"
    )
    op.drop_table("audit_events")
```

- [ ] **Step 6: Verify the migration applies cleanly against a scratch DB**

Run: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: no errors; `audit_events` exists at head. (If `alembic` points at the dev DB, this is the standard verification the repo uses; ensure `DATABASE_URL` is the intended target first.)

- [ ] **Step 7: Commit**

```bash
git add gatekeep/models.py migrations/versions/0024_audit_event.py tests/test_audit.py
git commit -m "feat(audit): add append-only audit_events table"
```

---

## Task 2: `record_audit_event` helper

**Files:**
- Create: `gatekeep/audit.py`
- Test: `tests/test_audit.py` (extend)

**Interfaces:**
- Consumes: `AuditEvent` (Task 1).
- Produces:
  ```python
  async def record_audit_event(
      session: AsyncSession,
      *,
      actor_account_id: int | None,
      actor_label: str,
      action: str,
      entity_type: str,
      entity_ref: str | None,
      result: str,
      version_num: int | None = None,
      details: dict | None = None,
  ) -> AuditEvent
  ```
  Adds and commits one row; returns it refreshed.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audit.py`:

```python
from gatekeep.audit import record_audit_event
from tests.helpers import create_account


async def test_record_audit_event_persists_and_commits(session):
    actor = await create_account(session, name="ops", is_operator=True)
    await session.commit()

    event = await record_audit_event(
        session,
        actor_account_id=actor.id,
        actor_label=actor.name,
        action="prompt.create",
        entity_type="prompt",
        entity_ref="greeting",
        result="success",
        details={"template_len": 12},
    )

    assert event.id is not None
    assert event.result == "success"
    assert event.actor_label == "ops"
    assert event.version_num is None
    assert event.details == {"template_len": 12}


async def test_record_audit_event_defaults_details_to_empty_dict(session):
    event = await record_audit_event(
        session,
        actor_account_id=None,
        actor_label="ops",
        action="eval.run",
        entity_type="prompt",
        entity_ref="greeting",
        result="error",
    )
    assert event.details == {}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_audit.py -k record_audit -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.audit'`.

- [ ] **Step 3: Implement `gatekeep/audit.py`**

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.models import AuditEvent


async def record_audit_event(
    session: AsyncSession,
    *,
    actor_account_id: int | None,
    actor_label: str,
    action: str,
    entity_type: str,
    entity_ref: str | None,
    result: str,
    version_num: int | None = None,
    details: dict | None = None,
) -> AuditEvent:
    """Append one audit event and commit it.

    This is the single writer of `audit_events` rows. It lives in the
    endpoint/job layer, never inside the pure service functions: each
    endpoint calls its service, then records the outcome here. `details`
    defaults to an empty dict.

    Args:
        session: The async DB session to write through.
        actor_account_id: The acting operator's account id, or None if the
            account is unknown/deleted.
        actor_label: The operator's name denormalized at action time.
        action: Namespaced verb, e.g. `prompt.promote`, `eval.run`.
        entity_type: The target entity kind, e.g. `prompt`, `eval_suite`.
        entity_ref: Human id of the target (e.g. prompt name), or None.
        result: One of `success`, `blocked`, `error`.
        version_num: Target version number where relevant.
        details: Action-specific JSON payload; defaults to `{}`.

    Returns:
        The persisted `AuditEvent`, refreshed.
    """
    event = AuditEvent(
        actor_account_id=actor_account_id,
        actor_label=actor_label,
        action=action,
        entity_type=entity_type,
        entity_ref=entity_ref,
        version_num=version_num,
        result=result,
        details=details or {},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_audit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/audit.py tests/test_audit.py
git commit -m "feat(audit): add record_audit_event helper"
```

---

## Task 3: Extend `GET /prompts/{name}/versions` with template text

**Files:**
- Modify: `gatekeep/api/dashboard.py` (`PromptVersionOut` ~line 1179, `prompt_version_timeline` ~line 1196)
- Test: `tests/test_dashboard.py` (extend near the existing timeline tests, ~line 690)

**Interfaces:**
- Produces: `PromptVersionOut` gains a `template: str` field, populated from `PromptVersion.template`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard.py`:

```python
async def test_prompt_versions_timeline_includes_template_text(
    client, operator_key, session
):
    await create_prompt("dash-tmpl-prompt", "the v1 template", session)
    await add_prompt_version("dash-tmpl-prompt", "the v2 template", session)

    r = await client.get(
        "/dashboard/api/prompts/dash-tmpl-prompt/versions",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert versions[0]["template"] == "the v1 template"
    assert versions[1]["template"] == "the v2 template"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_dashboard.py::test_prompt_versions_timeline_includes_template_text -v`
Expected: FAIL with `KeyError: 'template'`.

- [ ] **Step 3: Add `template` to the model and the response builder**

In `PromptVersionOut` add the field:

```python
class PromptVersionOut(BaseModel):
    """One immutable version in a prompt's promotion timeline."""

    version_num: int
    active: bool
    template: str
    created_at: datetime
    created_by: str | None
    notes: str | None
```

In `prompt_version_timeline`, set `template=v.template` in the comprehension:

```python
    versions = [
        PromptVersionOut(
            version_num=v.version_num,
            active=v.active,
            template=v.template,
            created_at=v.created_at,
            created_by=v.created_by,
            notes=v.notes,
        )
        for v in rows
    ]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_dashboard.py -k prompt_versions_timeline -v`
Expected: PASS (all timeline tests still green).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): include version template text in timeline"
```

---

## Task 4: Read endpoints for suite + curation

**Files:**
- Modify: `gatekeep/api/dashboard.py` (add imports, response models, two GET routes near the prompt reads, ~line 1237)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `evals.get_suite_for_prompt`, `curation.list_unreviewed`, models `EvalSuite`, `EvalCase`.
- Produces:
  - `GET /prompts/{name}/suite` -> `PromptSuiteResponse{ suite: SuiteOut | None, cases: list[EvalCaseOut] }`
  - `GET /prompts/{name}/curation` -> `CurationResponse{ cases: list[EvalCaseOut] }`
  - `SuiteOut{ id, name, prompt_name, pass_threshold, created_at }`
  - `EvalCaseOut{ id, check_type, expected, judge_criteria, reviewed, source, account_id, created_at, input_messages }`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`:

```python
async def test_prompt_suite_returns_suite_and_reviewed_cases(
    client, operator_key, session
):
    await create_prompt("dash-suite-prompt", "tmpl", session)
    suite = await create_suite("dash-suite-prompt", session, pass_threshold=0.7)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains",
        expected="hello",
    )

    r = await client.get(
        "/dashboard/api/prompts/dash-suite-prompt/suite",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["suite"]["pass_threshold"] == 0.7
    assert len(body["cases"]) == 1
    assert body["cases"][0]["check_type"] == "contains"


async def test_prompt_suite_null_when_no_suite(client, operator_key, session):
    await create_prompt("dash-nosuite-prompt", "tmpl", session)
    r = await client.get(
        "/dashboard/api/prompts/dash-nosuite-prompt/suite",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    assert r.json() == {"suite": None, "cases": []}


async def test_prompt_curation_lists_unreviewed_only(client, operator_key, session):
    await create_prompt("dash-cur-prompt", "tmpl", session)
    suite = await create_suite("dash-cur-prompt", session, pass_threshold=0.5)
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "q"}],
        check_type="llm_judge",
        judge_criteria="ok",
        reviewed=False,
        source="curated",
    )
    await add_case(
        suite.id,
        session,
        input_messages=[{"role": "user", "content": "q2"}],
        check_type="llm_judge",
        judge_criteria="ok",
        reviewed=True,
    )
    r = await client.get(
        "/dashboard/api/prompts/dash-cur-prompt/curation",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    cases = r.json()["cases"]
    assert len(cases) == 1
    assert cases[0]["reviewed"] is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_dashboard.py -k "prompt_suite or prompt_curation" -v`
Expected: FAIL (404, routes not defined).

- [ ] **Step 3: Add imports, models, and the two routes**

Extend the model import in `dashboard.py` to include `EvalCase`, `EvalSuite` (EvalSuite already imported; add `EvalCase`), and import the services:

```python
from gatekeep.curation import list_unreviewed
from gatekeep.evals import get_suite_for_prompt
from gatekeep.models import (
    Account,
    ApiKey,
    AuditEvent,
    EvalCase,
    EvalRun,
    EvalSuite,
    Prompt,
    PromptVersion,
    RequestLog,
)
```

Add response models and routes (place after `prompt_version_timeline`):

```python
class SuiteOut(BaseModel):
    """An eval suite bound to a prompt."""

    id: int
    name: str
    prompt_name: str
    pass_threshold: float
    created_at: datetime


class EvalCaseOut(BaseModel):
    """One eval case in a suite, reviewed or curated-and-unreviewed."""

    id: int
    check_type: str
    expected: str | None
    judge_criteria: str | None
    reviewed: bool
    source: str
    account_id: int | None
    created_at: datetime
    input_messages: list[dict]


class PromptSuiteResponse(BaseModel):
    """A prompt's eval suite and its cases, or null suite / empty cases."""

    suite: SuiteOut | None
    cases: list[EvalCaseOut]


class CurationResponse(BaseModel):
    """A prompt's unreviewed curated cases."""

    cases: list[EvalCaseOut]


def _case_out(case: EvalCase) -> EvalCaseOut:
    """Map an EvalCase ORM row to its response model."""
    return EvalCaseOut(
        id=case.id,
        check_type=case.check_type,
        expected=case.expected,
        judge_criteria=case.judge_criteria,
        reviewed=case.reviewed,
        source=case.source,
        account_id=case.account_id,
        created_at=case.created_at,
        input_messages=case.input_messages,
    )


@router.get("/prompts/{name}/suite", response_model=PromptSuiteResponse)
async def prompt_suite(
    name: str,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> PromptSuiteResponse:
    """Return the eval suite bound to `name` and its cases.

    Returns `{suite: null, cases: []}` when no suite is registered (not a
    404) - the UI treats "no suite" as an offer to create one. Operator only.
    """
    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        return PromptSuiteResponse(suite=None, cases=[])
    rows = (
        (
            await session.execute(
                select(EvalCase)
                .where(EvalCase.suite_id == suite.id)
                .order_by(EvalCase.id)
            )
        )
        .scalars()
        .all()
    )
    return PromptSuiteResponse(
        suite=SuiteOut(
            id=suite.id,
            name=suite.name,
            prompt_name=suite.prompt_name,
            pass_threshold=suite.pass_threshold,
            created_at=suite.created_at,
        ),
        cases=[_case_out(c) for c in rows],
    )


@router.get("/prompts/{name}/curation", response_model=CurationResponse)
async def prompt_curation(
    name: str,
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> CurationResponse:
    """Return `name`'s unreviewed curated eval cases, oldest first. Operator only."""
    cases = await list_unreviewed(name, session)
    return CurationResponse(cases=[_case_out(c) for c in cases])
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard.py -k "prompt_suite or prompt_curation" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add suite and curation read endpoints"
```

---

## Task 5: `GET /audit` read feed

**Files:**
- Modify: `gatekeep/api/dashboard.py` (models + route)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `AuditEvent`, `record_audit_event` (for seeding in tests).
- Produces: `GET /audit?entity_type=&entity_ref=&action=&limit=` -> `AuditFeedResponse{ events: list[AuditEventOut] }`, newest first. `AuditEventOut{ id, created_at, actor_account_id, actor_label, action, entity_type, entity_ref, version_num, result, details }`.

- [ ] **Step 1: Write the failing test**

```python
async def test_audit_feed_filters_and_orders_newest_first(
    client, operator_key, session
):
    from gatekeep.audit import record_audit_event

    await record_audit_event(
        session, actor_account_id=None, actor_label="op", action="prompt.create",
        entity_type="prompt", entity_ref="p1", result="success",
    )
    await record_audit_event(
        session, actor_account_id=None, actor_label="op", action="prompt.promote",
        entity_type="prompt", entity_ref="p1", result="success", version_num=2,
    )
    await record_audit_event(
        session, actor_account_id=None, actor_label="op", action="prompt.promote",
        entity_type="prompt", entity_ref="p2", result="blocked",
    )

    r = await client.get(
        "/dashboard/api/audit",
        headers={"Authorization": f"Bearer {operator_key}"},
        params={"entity_type": "prompt", "entity_ref": "p1"},
    )
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["action"] for e in events] == ["prompt.promote", "prompt.create"]
    assert events[0]["version_num"] == 2


async def test_audit_feed_requires_operator(client, raw_key):
    r = await client.get(
        "/dashboard/api/audit", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert r.status_code == 403
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_dashboard.py -k audit_feed -v`
Expected: FAIL (route missing -> 404/403 mismatch).

- [ ] **Step 3: Add models + route**

```python
class AuditEventOut(BaseModel):
    """One audit-log row for the read-only feed."""

    id: int
    created_at: datetime
    actor_account_id: int | None
    actor_label: str
    action: str
    entity_type: str
    entity_ref: str | None
    version_num: int | None
    result: str
    details: dict


class AuditFeedResponse(BaseModel):
    """A page of audit events, newest first."""

    events: list[AuditEventOut]


@router.get("/audit", response_model=AuditFeedResponse)
async def audit_feed(
    entity_type: str | None = Query(default=None),
    entity_ref: str | None = Query(default=None),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _operator: Account = Depends(require_operator),
) -> AuditFeedResponse:
    """Return the audit feed, newest first, filterable by entity/action.

    Fleet-wide and operator only. `entity_type`/`entity_ref`/`action` are
    optional equality filters; `limit` caps the page (default 100, max 500).
    """
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    if entity_type is not None:
        query = query.where(AuditEvent.entity_type == entity_type)
    if entity_ref is not None:
        query = query.where(AuditEvent.entity_ref == entity_ref)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    query = query.limit(limit)

    rows = (await session.execute(query)).scalars().all()
    return AuditFeedResponse(
        events=[
            AuditEventOut(
                id=e.id,
                created_at=e.created_at,
                actor_account_id=e.actor_account_id,
                actor_label=e.actor_label,
                action=e.action,
                entity_type=e.entity_type,
                entity_ref=e.entity_ref,
                version_num=e.version_num,
                result=e.result,
                details=e.details,
            )
            for e in rows
        ]
    )
```

Note the secondary `AuditEvent.id.desc()` ordering keeps same-timestamp rows deterministic (matches `recent_samples`).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard.py -k audit_feed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add audit read feed endpoint"
```

---

## Task 6: Synchronous prompt-version mutations (create / add-version / rollback)

**Files:**
- Modify: `gatekeep/api/dashboard.py` (imports, request models, three routes)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `prompts.create_prompt`, `add_prompt_version`, `rollback_prompt`, `PromptNotFoundError`; `audit.record_audit_event`; `_get_redis` (existing dependency).
- Produces:
  - `POST /prompts` body `{name, template, notes?}` -> `PromptMutationResponse{ name, version_num }`, audit `prompt.create`.
  - `POST /prompts/{name}/versions` body `{template, notes?}` -> `PromptMutationResponse`, audit `prompt.add_version`.
  - `POST /prompts/{name}/rollback` -> `PromptMutationResponse`, audit `prompt.rollback`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_create_prompt_persists_and_audits(client, operator_key, session):
    r = await client.post(
        "/dashboard/api/prompts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "new-prompt", "template": "hello", "notes": "first"},
    )
    assert r.status_code == 200
    assert r.json() == {"name": "new-prompt", "version_num": 1}

    from gatekeep.models import AuditEvent
    from sqlalchemy import select as _select

    events = (
        await session.execute(
            _select(AuditEvent).where(AuditEvent.entity_ref == "new-prompt")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].action == "prompt.create"
    assert events[0].result == "success"
    assert events[0].actor_label == "op-acct"


async def test_create_prompt_sets_created_by_to_operator(client, operator_key, session):
    await client.post(
        "/dashboard/api/prompts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "attrib-prompt", "template": "hi"},
    )
    from gatekeep.prompts import get_active_prompt_version

    version = await get_active_prompt_version("attrib-prompt", session)
    assert version.created_by == "op-acct"


async def test_create_prompt_duplicate_is_400(client, operator_key, session):
    await create_prompt("dupe-prompt", "x", session)
    r = await client.post(
        "/dashboard/api/prompts",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"name": "dupe-prompt", "template": "y"},
    )
    assert r.status_code == 400


async def test_add_version_appends_inactive(client, operator_key, session):
    await create_prompt("addver-prompt", "v1", session)
    r = await client.post(
        "/dashboard/api/prompts/addver-prompt/versions",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"template": "v2", "notes": "second"},
    )
    assert r.status_code == 200
    assert r.json()["version_num"] == 2


async def test_add_version_unknown_prompt_is_404(client, operator_key):
    r = await client.post(
        "/dashboard/api/prompts/ghost/versions",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"template": "v2"},
    )
    assert r.status_code == 404


async def test_rollback_reverts_and_audits(client, operator_key, session):
    await create_prompt("rb-prompt", "v1", session)
    await add_prompt_version("rb-prompt", "v2", session)
    await promote_prompt("rb-prompt", 2, session)

    r = await client.post(
        "/dashboard/api/prompts/rb-prompt/rollback",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    assert r.json()["version_num"] == 1


async def test_rollback_without_history_is_400(client, operator_key, session):
    await create_prompt("rb-none", "v1", session)
    r = await client.post(
        "/dashboard/api/prompts/rb-none/rollback",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_dashboard.py -k "create_prompt or add_version or rollback" -v`
Expected: FAIL (routes missing).

- [ ] **Step 3: Add imports, request model, and the three routes**

Add imports at the top of `dashboard.py`:

```python
from gatekeep.audit import record_audit_event
from gatekeep.prompts import (
    PromptNotFoundError,
    PromptVersionNotFoundError,
    _get_prompt_row,
    add_prompt_version,
    clear_candidate_version,
    create_prompt,
    rollback_prompt,
    set_candidate_version,
)
```

(Consolidate with the existing `from gatekeep.prompts import PromptNotFoundError, _get_prompt_row` line - replace it with the block above.)

Add request/response models and routes:

```python
class PromptCreateRequest(BaseModel):
    """Request body for creating a prompt (initial version 1, active)."""

    name: str
    template: str
    notes: str | None = None


class PromptVersionCreateRequest(BaseModel):
    """Request body for appending a new inactive version to a prompt."""

    template: str
    notes: str | None = None


class PromptMutationResponse(BaseModel):
    """The prompt name and the version number a mutation produced/left active."""

    name: str
    version_num: int


@router.post("/prompts", response_model=PromptMutationResponse)
async def create_prompt_route(
    body: PromptCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Create a prompt with an initial active version 1. Operator only.

    Sets `created_by` to the operator's account name and records a
    `prompt.create` audit event. 400 if the name already exists.
    """
    try:
        prompt = await create_prompt(
            body.name,
            body.template,
            session,
            created_by=operator.name,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.create",
        entity_type="prompt",
        entity_ref=body.name,
        version_num=1,
        result="success",
        details={"notes": body.notes},
    )
    return PromptMutationResponse(name=prompt.name, version_num=1)


@router.post("/prompts/{name}/versions", response_model=PromptMutationResponse)
async def add_prompt_version_route(
    name: str,
    body: PromptVersionCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Append a new inactive version to an existing prompt. Operator only.

    Sets `created_by` to the operator's account name and records a
    `prompt.add_version` audit event. 404 if the prompt is unknown.
    """
    try:
        version = await add_prompt_version(
            name, body.template, session, created_by=operator.name, notes=body.notes
        )
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.add_version",
        entity_type="prompt",
        entity_ref=name,
        version_num=version.version_num,
        result="success",
        details={"notes": body.notes},
    )
    return PromptMutationResponse(name=name, version_num=version.version_num)


@router.post("/prompts/{name}/rollback", response_model=PromptMutationResponse)
async def rollback_prompt_route(
    name: str,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(_get_redis),
    operator: Account = Depends(require_operator),
) -> PromptMutationResponse:
    """Revert a prompt to its previously-active version. Operator only.

    Rollback is never eval-gated (reverting to an already-proven version).
    Invalidates the prompt's caches via the service layer and records a
    `prompt.rollback` audit event. 404 if the prompt is unknown, 400 if
    there is no earlier version to roll back to.
    """
    try:
        version = await rollback_prompt(name, session, redis=redis)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.rollback",
        entity_type="prompt",
        entity_ref=name,
        version_num=version.version_num,
        result="success",
        details={"to_version": version.version_num},
    )
    return PromptMutationResponse(name=name, version_num=version.version_num)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard.py -k "create_prompt or add_version or rollback" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add prompt create/add-version/rollback mutations"
```

---

## Task 7: A/B candidate mutations (set / clear)

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `prompts.set_candidate_version`, `clear_candidate_version` (imported in Task 6).
- Produces:
  - `PUT /prompts/{name}/candidate` body `{version_num, traffic_pct}` -> `CandidateResponse{ name, candidate_version_num, traffic_pct }`, audit `prompt.set_candidate`.
  - `DELETE /prompts/{name}/candidate` -> `CandidateResponse{ name, candidate_version_num: null, traffic_pct: null }`, audit `prompt.clear_candidate`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_set_candidate_configures_split(client, operator_key, session):
    await create_prompt("cand-prompt", "v1", session)
    await add_prompt_version("cand-prompt", "v2", session)
    r = await client.put(
        "/dashboard/api/prompts/cand-prompt/candidate",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"version_num": 2, "traffic_pct": 25},
    )
    assert r.status_code == 200
    assert r.json() == {
        "name": "cand-prompt",
        "candidate_version_num": 2,
        "traffic_pct": 25.0,
    }


async def test_set_candidate_invalid_pct_is_400(client, operator_key, session):
    await create_prompt("cand-bad", "v1", session)
    r = await client.put(
        "/dashboard/api/prompts/cand-bad/candidate",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"version_num": 1, "traffic_pct": 150},
    )
    assert r.status_code == 400


async def test_set_candidate_unknown_version_is_404(client, operator_key, session):
    await create_prompt("cand-nov", "v1", session)
    r = await client.put(
        "/dashboard/api/prompts/cand-nov/candidate",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"version_num": 9, "traffic_pct": 10},
    )
    assert r.status_code == 404


async def test_clear_candidate_resets(client, operator_key, session):
    await create_prompt("cand-clear", "v1", session)
    await add_prompt_version("cand-clear", "v2", session)
    await set_candidate_version("cand-clear", 2, 30, session)
    r = await client.delete(
        "/dashboard/api/prompts/cand-clear/candidate",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 200
    assert r.json()["candidate_version_num"] is None
    assert r.json()["traffic_pct"] is None
```

Add `set_candidate_version` to the `from gatekeep.prompts import ...` line in the test module imports at top of `tests/test_dashboard.py`.

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_dashboard.py -k candidate -v`
Expected: FAIL.

- [ ] **Step 3: Add models + routes**

The service returns a `Prompt` whose `candidate_version_id` is a `PromptVersion.id`, not a `version_num`. Resolve the version number for the response by loading that row.

```python
class CandidateSetRequest(BaseModel):
    """Request body for setting/adjusting a prompt's A/B candidate."""

    version_num: int
    traffic_pct: float


class CandidateResponse(BaseModel):
    """A prompt's current A/B candidate config (null when none)."""

    name: str
    candidate_version_num: int | None
    traffic_pct: float | None


async def _candidate_response(name: str, prompt: Prompt, session: AsyncSession) -> CandidateResponse:
    """Build a CandidateResponse, resolving candidate_version_id to a version_num."""
    version_num = None
    if prompt.candidate_version_id is not None:
        version = await session.get(PromptVersion, prompt.candidate_version_id)
        version_num = version.version_num if version is not None else None
    return CandidateResponse(
        name=name,
        candidate_version_num=version_num,
        traffic_pct=prompt.candidate_traffic_pct,
    )


@router.put("/prompts/{name}/candidate", response_model=CandidateResponse)
async def set_candidate_route(
    name: str,
    body: CandidateSetRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CandidateResponse:
    """Configure or adjust a prompt's A/B candidate version + traffic split.

    Never runs the eval gate or invalidates cache (a candidate is not
    "active"). Records a `prompt.set_candidate` audit event. 404 if the
    prompt or version is unknown, 400 if `traffic_pct` is outside [0, 100].
    """
    try:
        prompt = await set_candidate_version(name, body.version_num, body.traffic_pct, session)
    except (PromptNotFoundError, PromptVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.set_candidate",
        entity_type="prompt",
        entity_ref=name,
        version_num=body.version_num,
        result="success",
        details={"traffic_pct": body.traffic_pct},
    )
    return await _candidate_response(name, prompt, session)


@router.delete("/prompts/{name}/candidate", response_model=CandidateResponse)
async def clear_candidate_route(
    name: str,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CandidateResponse:
    """Clear a prompt's A/B candidate (100% traffic back to active). Operator only.

    Records a `prompt.clear_candidate` audit event. 404 if the prompt is
    unknown. A no-op (but still success) when no candidate was configured.
    """
    try:
        prompt = await clear_candidate_version(name, session)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="prompt.clear_candidate",
        entity_type="prompt",
        entity_ref=name,
        result="success",
    )
    return await _candidate_response(name, prompt, session)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard.py -k candidate -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add A/B candidate set/clear mutations"
```

---

## Task 8: Eval-suite + curation sync mutations (create-suite / add-case / mine / review)

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `evals.create_suite`, `add_case`; `curation.curate_cases`, `review_case`; the eval provider dependency `_get_eval_provider` (introduced here). `get_settings` (already imported).
- Produces:
  - `POST /prompts/{name}/suite` body `{threshold?}` -> `SuiteOut`, audit `eval.create_suite`.
  - `POST /prompts/{name}/suite/cases` body `{input_messages, check_type, expected?, judge_criteria?}` -> `EvalCaseOut`, audit `eval.add_case`.
  - `POST /prompts/{name}/curation/mine` body `{limit?}` -> `CurationResponse`, audit `curation.mine` (result="success", details `{case_count}`).
  - `POST /prompts/{name}/curation/{case_id}/review` body `{approved}` -> `{status: "reviewed"}`, audit `curation.review`.
  - `_get_eval_provider() -> AnthropicProvider` dependency (module-level, overridable in tests).

- [ ] **Step 1: Write the failing tests**

```python
async def test_create_suite_defaults_threshold_from_settings(client, operator_key, session):
    await create_prompt("suite-create", "v1", session)
    r = await client.post(
        "/dashboard/api/prompts/suite-create/suite",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={},
    )
    assert r.status_code == 200
    assert r.json()["prompt_name"] == "suite-create"
    assert r.json()["pass_threshold"] == 0.9  # eval_pass_threshold_default


async def test_add_case_contains_requires_expected(client, operator_key, session):
    await create_prompt("case-prompt", "v1", session)
    await create_suite("case-prompt", session, pass_threshold=0.5)
    r = await client.post(
        "/dashboard/api/prompts/case-prompt/suite/cases",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"input_messages": [{"role": "user", "content": "hi"}], "check_type": "contains"},
    )
    assert r.status_code == 400


async def test_add_case_persists_reviewed_case(client, operator_key, session):
    await create_prompt("case-ok", "v1", session)
    await create_suite("case-ok", session, pass_threshold=0.5)
    r = await client.post(
        "/dashboard/api/prompts/case-ok/suite/cases",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={
            "input_messages": [{"role": "user", "content": "hi"}],
            "check_type": "contains",
            "expected": "hello",
        },
    )
    assert r.status_code == 200
    assert r.json()["reviewed"] is True
    assert r.json()["source"] == "manual"


async def test_curation_review_approves_case(client, operator_key, session):
    await create_prompt("rev-prompt", "v1", session)
    suite = await create_suite("rev-prompt", session, pass_threshold=0.5)
    case = await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "q"}],
        check_type="llm_judge", judge_criteria="ok", reviewed=False, source="curated",
    )
    r = await client.post(
        f"/dashboard/api/prompts/rev-prompt/curation/{case.id}/review",
        headers={"Authorization": f"Bearer {operator_key}"},
        json={"approved": True},
    )
    assert r.status_code == 200
    from gatekeep.models import EvalCase as _EvalCase
    refreshed = await session.get(_EvalCase, case.id)
    assert refreshed.reviewed is True


async def test_curation_mine_uses_injected_provider(client, operator_key, session):
    from gatekeep.app import app
    from gatekeep.api.dashboard import _get_eval_provider
    from gatekeep.samples import record_request_sample

    await create_prompt("mine-prompt", "v1", session)
    await create_suite("mine-prompt", session, pass_threshold=0.5)
    # a key/account to attribute the sample to
    from gatekeep.models import ApiKey as _ApiKey
    acct = (await session.execute(select(_ApiKey))).scalars().first()
    await record_request_sample(
        session, key_id=acct.id, account_id=acct.account_id,
        prompt_name="mine-prompt", model="claude-sonnet-5",
        input_messages=[{"role": "user", "content": "hi"}], output_text="hello there",
    )

    class _FakeProvider:
        async def complete(self, payload):
            class R:
                text = "generated rubric"
            return R()

    app.dependency_overrides[_get_eval_provider] = lambda: _FakeProvider()
    try:
        r = await client.post(
            "/dashboard/api/prompts/mine-prompt/curation/mine",
            headers={"Authorization": f"Bearer {operator_key}"},
            json={"limit": 5},
        )
    finally:
        app.dependency_overrides.pop(_get_eval_provider, None)
    assert r.status_code == 200
    assert len(r.json()["cases"]) == 1
    assert r.json()["cases"][0]["source"] == "curated"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_dashboard.py -k "create_suite or add_case or curation_review or curation_mine" -v`
Expected: FAIL.

- [ ] **Step 3: Add the eval-provider dependency, request models, and routes**

Add imports:

```python
from anthropic import AsyncAnthropic

from gatekeep.curation import curate_cases, review_case
from gatekeep.evals import add_case, create_suite, get_suite_for_prompt
from gatekeep.providers.anthropic import AnthropicProvider
```

Add the provider dependency (near `_get_redis`):

```python
def _get_eval_provider() -> AnthropicProvider:
    """FastAPI dependency yielding the provider used for eval/curation LLM calls.

    Mirrors how the CLI builds its provider (`AnthropicProvider(AsyncAnthropic(...))`).
    Isolated as a dependency so tests can override it with a fake provider via
    `app.dependency_overrides`, keeping eval/curation endpoints and their
    background jobs off the real Anthropic API in the suite.
    """
    settings = get_settings()
    return AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key))
```

Add request models + routes:

```python
class SuiteCreateRequest(BaseModel):
    """Request body for creating an eval suite (threshold defaults from settings)."""

    threshold: float | None = None


class CaseCreateRequest(BaseModel):
    """Request body for adding a reviewed manual eval case."""

    input_messages: list[dict]
    check_type: str
    expected: str | None = None
    judge_criteria: str | None = None


class CurationMineRequest(BaseModel):
    """Request body for mining recent samples into unreviewed curated cases."""

    limit: int = 20


class CurationReviewRequest(BaseModel):
    """Request body for approving/rejecting one curated case."""

    approved: bool


@router.post("/prompts/{name}/suite", response_model=SuiteOut)
async def create_suite_route(
    name: str,
    body: SuiteCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> SuiteOut:
    """Create an eval suite for a prompt (one per prompt). Operator only.

    Threshold defaults to `eval_pass_threshold_default`. Records an
    `eval.create_suite` audit event. 400 if a suite already exists.
    """
    threshold = (
        body.threshold
        if body.threshold is not None
        else get_settings().eval_pass_threshold_default
    )
    try:
        suite = await create_suite(name, session, pass_threshold=threshold)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=400,
            detail=_error_body(f"an eval suite already exists for prompt {name!r}"),
        ) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="eval.create_suite",
        entity_type="eval_suite",
        entity_ref=name,
        result="success",
        details={"threshold": threshold},
    )
    return SuiteOut(
        id=suite.id,
        name=suite.name,
        prompt_name=suite.prompt_name,
        pass_threshold=suite.pass_threshold,
        created_at=suite.created_at,
    )


@router.post("/prompts/{name}/suite/cases", response_model=EvalCaseOut)
async def add_case_route(
    name: str,
    body: CaseCreateRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> EvalCaseOut:
    """Add a reviewed manual eval case to a prompt's suite. Operator only.

    Records an `eval.add_case` audit event. 404 if no suite is registered,
    400 if the check_type/argument combination is invalid.
    """
    suite = await get_suite_for_prompt(name, session)
    if suite is None:
        raise HTTPException(
            status_code=404,
            detail=_error_body(f"no eval suite registered for prompt {name!r}"),
        )
    try:
        case = await add_case(
            suite.id,
            session,
            input_messages=body.input_messages,
            check_type=body.check_type,
            expected=body.expected,
            judge_criteria=body.judge_criteria,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="eval.add_case",
        entity_type="eval_suite",
        entity_ref=name,
        result="success",
        details={"check_type": body.check_type, "case_id": case.id},
    )
    return _case_out(case)


@router.post("/prompts/{name}/curation/mine", response_model=CurationResponse)
async def curation_mine_route(
    name: str,
    body: CurationMineRequest,
    session: AsyncSession = Depends(get_session),
    provider: AnthropicProvider = Depends(_get_eval_provider),
    operator: Account = Depends(require_operator),
) -> CurationResponse:
    """Mine recent request samples for a prompt into unreviewed curated cases.

    Operator only. Uses the eval provider to draft a judge rubric per sample.
    Records a `curation.mine` audit event with the mined case count. 404 if
    no suite is registered.
    """
    try:
        cases = await curate_cases(
            name,
            session,
            limit=body.limit,
            provider=provider,
            generate_model=get_settings().default_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="curation.mine",
        entity_type="prompt",
        entity_ref=name,
        result="success",
        details={"case_count": len(cases)},
    )
    return CurationResponse(cases=[_case_out(c) for c in cases])


class CurationReviewResponse(BaseModel):
    """Terminal state of a reviewed curated case."""

    status: str


@router.post(
    "/prompts/{name}/curation/{case_id}/review",
    response_model=CurationReviewResponse,
)
async def curation_review_route(
    name: str,
    case_id: int,
    body: CurationReviewRequest,
    session: AsyncSession = Depends(get_session),
    operator: Account = Depends(require_operator),
) -> CurationReviewResponse:
    """Approve (keep, mark reviewed) or reject (delete) one curated case.

    Operator only. Records a `curation.review` audit event. 404 if the case
    id does not exist.
    """
    try:
        await review_case(case_id, session, approve=body.approved)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_error_body(str(exc))) from exc
    await record_audit_event(
        session,
        actor_account_id=operator.id,
        actor_label=operator.name,
        action="curation.review",
        entity_type="curated_case",
        entity_ref=name,
        result="success",
        details={"case_id": case_id, "approved": body.approved},
    )
    return CurationReviewResponse(status="reviewed" if body.approved else "rejected")
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_dashboard.py -k "create_suite or add_case or curation_review or curation_mine" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add eval-suite and curation sync mutations"
```

---

## Task 9: Background-job channel (`promptjobs.py`) + poll endpoint

**Files:**
- Create: `gatekeep/promptjobs.py`
- Modify: `gatekeep/api/dashboard.py` (poll route)
- Test: `tests/test_promptjobs.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis`.
- Produces:
  - `_JOB_KEY_PREFIX = "promptjob:"`, `_JOB_TTL_SECONDS = 3600`.
  - `async def create_job(redis, *, kind, prompt_name, version_num) -> str` - writes a `queued` record, returns the uuid.
  - `async def get_job(redis, job_id) -> dict | None`.
  - `async def update_job(redis, job_id, **changes) -> dict | None` - merges changes, bumps `updated_at`, re-sets TTL.
  - `def spawn(coro) -> asyncio.Task` - creates a task and holds a reference until done.
  - Job record shape: `{id, kind, prompt_name, version_num, status, progress: {done, total}, result: {score, passed} | None, error: str | None, created_at, updated_at}`.
  - `GET /prompts/jobs/{job_id}` -> `JobStatusResponse` (404 when missing/expired).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_promptjobs.py`:

```python
from __future__ import annotations

import pytest

from gatekeep import promptjobs
from gatekeep.middleware.ratelimit import get_redis


@pytest.fixture
def redis():
    return get_redis()


async def test_create_job_writes_queued_record(redis):
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name="p1", version_num=2
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["id"] == job_id
    assert job["kind"] == "eval_run"
    assert job["status"] == "queued"
    assert job["prompt_name"] == "p1"
    assert job["version_num"] == 2
    assert job["progress"] == {"done": 0, "total": 0}


async def test_update_job_merges_and_preserves_fields(redis):
    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name="p2", version_num=3
    )
    await promptjobs.update_job(
        redis, job_id, status="running", progress={"done": 1, "total": 4}
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "running"
    assert job["progress"] == {"done": 1, "total": 4}
    assert job["prompt_name"] == "p2"  # untouched


async def test_get_job_missing_returns_none(redis):
    assert await promptjobs.get_job(redis, "does-not-exist") is None
```

Add to `tests/test_dashboard.py`:

```python
async def test_job_poll_404_for_unknown_job(client, operator_key):
    r = await client.get(
        "/dashboard/api/prompts/jobs/nope",
        headers={"Authorization": f"Bearer {operator_key}"},
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_promptjobs.py tests/test_dashboard.py::test_job_poll_404_for_unknown_job -v`
Expected: FAIL (module/route missing).

- [ ] **Step 3: Implement `gatekeep/promptjobs.py`**

```python
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

_JOB_KEY_PREFIX = "promptjob:"
_JOB_TTL_SECONDS = 3600

# Hold strong references to in-flight background tasks so the event loop does
# not garbage-collect a task whose only reference was the local returned by
# asyncio.create_task (a documented footgun). Tasks remove themselves on
# completion via the done callback.
_tasks: set[asyncio.Task] = set()


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _key(job_id: str) -> str:
    """Return the Redis key holding the job record for `job_id`."""
    return f"{_JOB_KEY_PREFIX}{job_id}"


async def create_job(
    redis: Redis, *, kind: str, prompt_name: str, version_num: int | None
) -> str:
    """Create a `queued` job record in Redis and return its generated id.

    Args:
        redis: The async Redis client.
        kind: `eval_run` or `promote`.
        prompt_name: The prompt the job targets.
        version_num: The target version number, or None.

    Returns:
        The new job's uuid (hex string).
    """
    job_id = uuid4().hex
    record = {
        "id": job_id,
        "kind": kind,
        "prompt_name": prompt_name,
        "version_num": version_num,
        "status": "queued",
        "progress": {"done": 0, "total": 0},
        "result": None,
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    await redis.set(_key(job_id), json.dumps(record), ex=_JOB_TTL_SECONDS)
    return job_id


async def get_job(redis: Redis, job_id: str) -> dict | None:
    """Return the job record for `job_id`, or None if it is missing/expired."""
    raw = await redis.get(_key(job_id))
    if raw is None:
        return None
    return json.loads(raw)


async def update_job(redis: Redis, job_id: str, **changes: Any) -> dict | None:
    """Merge `changes` into a job record, bump `updated_at`, and reset its TTL.

    Returns the updated record, or None if the job no longer exists (expired
    or never created). The whole-record read-modify-write is safe here
    because a single job is only ever advanced by its one owning task.
    """
    record = await get_job(redis, job_id)
    if record is None:
        return None
    record.update(changes)
    record["updated_at"] = _now()
    await redis.set(_key(job_id), json.dumps(record), ex=_JOB_TTL_SECONDS)
    return record


def spawn(coro) -> asyncio.Task:
    """Schedule `coro` as a tracked background task.

    Keeps a strong reference in `_tasks` until the task finishes, so the
    event loop cannot drop a still-running job. This is the in-process
    Approach A backbone: a process restart loses an in-flight task (the
    operator re-runs it), while completed results persist in the DB.
    """
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
```

- [ ] **Step 4: Add the poll route to `dashboard.py`**

Add import `from gatekeep import promptjobs` and the models/route:

```python
class JobProgress(BaseModel):
    """A job's per-case progress counter."""

    done: int
    total: int


class JobResult(BaseModel):
    """A completed eval/promote job's outcome payload."""

    score: float | None = None
    passed: bool | None = None


class JobStatusResponse(BaseModel):
    """Poll response for one background job."""

    id: str
    kind: str
    prompt_name: str
    version_num: int | None
    status: str
    progress: JobProgress
    result: JobResult | None
    error: str | None
    created_at: str
    updated_at: str


@router.get("/prompts/jobs/{job_id}", response_model=JobStatusResponse)
async def poll_job(
    job_id: str,
    redis: Redis = Depends(_get_redis),
    _operator: Account = Depends(require_operator),
) -> JobStatusResponse:
    """Return the status of a background job. Operator only.

    404 when the job id is unknown or its TTL has lapsed (the UI renders
    this as "status unavailable, refresh").
    """
    record = await promptjobs.get_job(redis, job_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail=_error_body("job not found or expired")
        )
    return JobStatusResponse(**record)
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_promptjobs.py tests/test_dashboard.py::test_job_poll_404_for_unknown_job -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/promptjobs.py gatekeep/api/dashboard.py tests/test_promptjobs.py tests/test_dashboard.py
git commit -m "feat(promptjobs): add Redis-backed job channel and poll endpoint"
```

---

## Task 10: Async eval-run endpoint + background task

**Files:**
- Modify: `gatekeep/promptjobs.py` (add `run_eval_job`)
- Modify: `gatekeep/api/dashboard.py` (eval-run route)
- Test: `tests/test_promptjobs.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `promptjobs.create_job/update_job/spawn`; `evals.run_suite_for_prompt`; `audit.record_audit_event`; `db.SessionLocal`.
- Produces:
  - `async def run_eval_job(job_id, *, prompt_name, version_num, model, provider, judge_model, max_tokens, actor_account_id, actor_label, redis, session_factory) -> None`.
  - `POST /prompts/{name}/eval-run` body `{version_num?, model?}` -> `JobCreatedResponse{ job_id }` (202-style, returns 200 with job id).

- [ ] **Step 1: Write the failing test (job drives status + writes EvalRun + audit)**

Add to `tests/test_promptjobs.py`:

```python
from gatekeep.db import SessionLocal
from gatekeep.evals import add_case, create_suite
from gatekeep.models import AuditEvent, EvalRun
from gatekeep.prompts import create_prompt
from sqlalchemy import select


class _FakeProvider:
    async def complete(self, payload):
        class R:
            text = "hello"
        return R()


async def test_run_eval_job_succeeds_and_writes_run_and_audit(redis, session):
    await create_prompt("job-eval", "tmpl", session)
    suite = await create_suite("job-eval", session, pass_threshold=0.5)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains", expected="hello",
    )
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name="job-eval", version_num=None
    )
    await promptjobs.run_eval_job(
        job_id,
        prompt_name="job-eval",
        version_num=None,
        model="claude-sonnet-5",
        provider=_FakeProvider(),
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "succeeded"
    assert job["result"]["passed"] is True

    async with SessionLocal() as s:
        runs = (await s.execute(select(EvalRun))).scalars().all()
        assert len(runs) == 1
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "eval.run"))
        ).scalars().all()
        assert len(events) == 1
        assert events[0].result == "success"


async def test_run_eval_job_records_error_on_missing_suite(redis, session):
    await create_prompt("job-eval-nosuite", "tmpl", session)
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name="job-eval-nosuite", version_num=None
    )
    await promptjobs.run_eval_job(
        job_id,
        prompt_name="job-eval-nosuite",
        version_num=None,
        model="claude-sonnet-5",
        provider=_FakeProvider(),
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "failed"
    assert job["error"] is not None
    async with SessionLocal() as s:
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "eval.run"))
        ).scalars().all()
        assert events[0].result == "error"
```

Add the endpoint test to `tests/test_dashboard.py`:

```python
async def test_eval_run_endpoint_returns_job_id_and_completes(client, operator_key, session):
    from gatekeep.app import app
    from gatekeep.api.dashboard import _get_eval_provider
    from gatekeep.middleware.ratelimit import get_redis
    from gatekeep import promptjobs

    await create_prompt("ep-eval", "tmpl", session)
    suite = await create_suite("ep-eval", session, pass_threshold=0.5)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains", expected="hello",
    )

    class _FakeProvider:
        async def complete(self, payload):
            class R:
                text = "hello"
            return R()

    app.dependency_overrides[_get_eval_provider] = lambda: _FakeProvider()
    try:
        r = await client.post(
            "/dashboard/api/prompts/ep-eval/eval-run",
            headers={"Authorization": f"Bearer {operator_key}"},
            json={},
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        # Poll until the background task reaches a terminal state.
        import asyncio
        redis = get_redis()
        for _ in range(50):
            job = await promptjobs.get_job(redis, job_id)
            if job and job["status"] in ("succeeded", "failed", "blocked"):
                break
            await asyncio.sleep(0.1)
        assert job["status"] == "succeeded"
    finally:
        app.dependency_overrides.pop(_get_eval_provider, None)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_promptjobs.py -k run_eval_job tests/test_dashboard.py::test_eval_run_endpoint_returns_job_id_and_completes -v`
Expected: FAIL (`run_eval_job` / route missing).

- [ ] **Step 3: Implement `run_eval_job` in `promptjobs.py`**

Add imports at the top of `promptjobs.py`:

```python
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekeep.audit import record_audit_event
from gatekeep.evals import run_suite_for_prompt
```

Add the coroutine:

```python
async def run_eval_job(
    job_id: str,
    *,
    prompt_name: str,
    version_num: int | None,
    model: str,
    provider,
    judge_model: str,
    max_tokens: int,
    actor_account_id: int | None,
    actor_label: str,
    redis: Redis,
    session_factory: async_sessionmaker,
) -> None:
    """Run a prompt's eval suite as a background job, driving Redis + audit.

    Transitions the job `queued -> running`, runs the suite via the existing
    `run_suite_for_prompt` (which persists the EvalRun), then writes the
    terminal status and a single `eval.run` audit event. A raised error
    (e.g. no suite, provider failure) yields status `failed` and audit
    `result="error"`; success yields `succeeded` and `result="success"`.

    Uses its own DB session from `session_factory` (not the request's), since
    the request has already returned by the time this runs.
    """
    await update_job(redis, job_id, status="running")
    try:
        async with session_factory() as session:
            run = await run_suite_for_prompt(
                prompt_name,
                session,
                provider=provider,
                generate_model=model,
                judge_model=judge_model,
                max_tokens=max_tokens,
                version_num=version_num,
            )
            await record_audit_event(
                session,
                actor_account_id=actor_account_id,
                actor_label=actor_label,
                action="eval.run",
                entity_type="prompt",
                entity_ref=prompt_name,
                version_num=version_num,
                result="success",
                details={"score": run.score, "passed": run.passed, "run_id": run.id},
            )
        await update_job(
            redis,
            job_id,
            status="succeeded",
            result={"score": run.score, "passed": run.passed},
        )
    except Exception as exc:  # noqa: BLE001 - terminal outcome is recorded, not swallowed
        async with session_factory() as session:
            await record_audit_event(
                session,
                actor_account_id=actor_account_id,
                actor_label=actor_label,
                action="eval.run",
                entity_type="prompt",
                entity_ref=prompt_name,
                version_num=version_num,
                result="error",
                details={"error": str(exc)},
            )
        await update_job(redis, job_id, status="failed", error=str(exc))
```

- [ ] **Step 4: Add the eval-run route to `dashboard.py`**

Add imports `from gatekeep.db import SessionLocal` and models/route:

```python
class EvalRunRequest(BaseModel):
    """Request body for an on-demand eval run (async job)."""

    version_num: int | None = None
    model: str | None = None


class JobCreatedResponse(BaseModel):
    """The id of a background job the caller should poll."""

    job_id: str


@router.post("/prompts/{name}/eval-run", response_model=JobCreatedResponse)
async def eval_run_route(
    name: str,
    body: EvalRunRequest,
    redis: Redis = Depends(_get_redis),
    provider: AnthropicProvider = Depends(_get_eval_provider),
    operator: Account = Depends(require_operator),
) -> JobCreatedResponse:
    """Kick off an on-demand eval run as a background job. Operator only.

    Returns a job id immediately; the UI polls `GET /prompts/jobs/{id}`. The
    background task drives Redis status, persists the EvalRun, and writes the
    `eval.run` audit event with its outcome (success or error).
    """
    settings = get_settings()
    job_id = await promptjobs.create_job(
        redis, kind="eval_run", prompt_name=name, version_num=body.version_num
    )
    promptjobs.spawn(
        promptjobs.run_eval_job(
            job_id,
            prompt_name=name,
            version_num=body.version_num,
            model=body.model or settings.default_model,
            provider=provider,
            judge_model=settings.eval_judge_model,
            max_tokens=settings.default_max_tokens,
            actor_account_id=operator.id,
            actor_label=operator.name,
            redis=redis,
            session_factory=SessionLocal,
        )
    )
    return JobCreatedResponse(job_id=job_id)
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_promptjobs.py tests/test_dashboard.py::test_eval_run_endpoint_returns_job_id_and_completes -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/promptjobs.py gatekeep/api/dashboard.py tests/test_promptjobs.py tests/test_dashboard.py
git commit -m "feat(dashboard): add async eval-run job endpoint"
```

---

## Task 11: Async promote endpoint + background task (eval gate -> blocked)

**Files:**
- Modify: `gatekeep/promptjobs.py` (add `run_promote_job`)
- Modify: `gatekeep/api/dashboard.py` (promote route)
- Test: `tests/test_promptjobs.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `prompts.promote_prompt`; `evals.make_eval_gate`, `EvalGateFailure`; `promptjobs` helpers; `record_audit_event`.
- Produces:
  - `async def run_promote_job(job_id, *, prompt_name, version_num, provider, generate_model, judge_model, max_tokens, actor_account_id, actor_label, redis, session_factory) -> None` - runs the gate then the flip; a gate failure yields status `blocked` + audit `result="blocked"`.
  - `POST /prompts/{name}/promote` body `{version_num}` -> `JobCreatedResponse`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_promptjobs.py`:

```python
from gatekeep.prompts import add_prompt_version, get_active_prompt_version


async def test_run_promote_job_no_suite_succeeds(redis, session):
    await create_prompt("job-promote", "v1", session)
    await add_prompt_version("job-promote", "v2", session)
    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name="job-promote", version_num=2
    )
    await promptjobs.run_promote_job(
        job_id,
        prompt_name="job-promote",
        version_num=2,
        provider=_FakeProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "succeeded"
    async with SessionLocal() as s:
        active = await get_active_prompt_version("job-promote", s)
        assert active.version_num == 2
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "prompt.promote"))
        ).scalars().all()
        assert events[0].result == "success"


async def test_run_promote_job_blocked_by_eval_gate(redis, session):
    await create_prompt("job-promote-gate", "v1", session)
    await add_prompt_version("job-promote-gate", "v2", session)
    suite = await create_suite("job-promote-gate", session, pass_threshold=1.0)
    await add_case(
        suite.id, session,
        input_messages=[{"role": "user", "content": "hi"}],
        check_type="contains", expected="WILL-NOT-MATCH",
    )

    class _FailProvider:
        async def complete(self, payload):
            class R:
                text = "nope"
            return R()

    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name="job-promote-gate", version_num=2
    )
    await promptjobs.run_promote_job(
        job_id,
        prompt_name="job-promote-gate",
        version_num=2,
        provider=_FailProvider(),
        generate_model="claude-sonnet-5",
        judge_model="claude-sonnet-5",
        max_tokens=64,
        actor_account_id=None,
        actor_label="op",
        redis=redis,
        session_factory=SessionLocal,
    )
    job = await promptjobs.get_job(redis, job_id)
    assert job["status"] == "blocked"
    assert job["result"]["passed"] is False
    async with SessionLocal() as s:
        active = await get_active_prompt_version("job-promote-gate", s)
        assert active.version_num == 1  # NOT promoted
        events = (
            await s.execute(select(AuditEvent).where(AuditEvent.action == "prompt.promote"))
        ).scalars().all()
        assert events[0].result == "blocked"
```

Add an endpoint test to `tests/test_dashboard.py`:

```python
async def test_promote_endpoint_returns_job_id(client, operator_key, session):
    from gatekeep.app import app
    from gatekeep.api.dashboard import _get_eval_provider
    from gatekeep.middleware.ratelimit import get_redis
    from gatekeep import promptjobs

    await create_prompt("ep-promote", "v1", session)
    await add_prompt_version("ep-promote", "v2", session)

    class _FakeProvider:
        async def complete(self, payload):
            class R:
                text = "x"
            return R()

    app.dependency_overrides[_get_eval_provider] = lambda: _FakeProvider()
    try:
        r = await client.post(
            "/dashboard/api/prompts/ep-promote/promote",
            headers={"Authorization": f"Bearer {operator_key}"},
            json={"version_num": 2},
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        import asyncio
        redis = get_redis()
        for _ in range(50):
            job = await promptjobs.get_job(redis, job_id)
            if job and job["status"] in ("succeeded", "failed", "blocked"):
                break
            await asyncio.sleep(0.1)
        assert job["status"] == "succeeded"
    finally:
        app.dependency_overrides.pop(_get_eval_provider, None)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_promptjobs.py -k run_promote_job tests/test_dashboard.py::test_promote_endpoint_returns_job_id -v`
Expected: FAIL.

- [ ] **Step 3: Implement `run_promote_job`**

Add imports to `promptjobs.py`:

```python
from gatekeep.evals import EvalGateFailure, make_eval_gate
from gatekeep.prompts import promote_prompt
```

Add the coroutine:

```python
async def run_promote_job(
    job_id: str,
    *,
    prompt_name: str,
    version_num: int,
    provider,
    generate_model: str,
    judge_model: str,
    max_tokens: int,
    actor_account_id: int | None,
    actor_label: str,
    redis: Redis,
    session_factory: async_sessionmaker,
) -> None:
    """Promote a prompt version as a background job, running the eval gate first.

    Builds the eval gate (a no-op when the prompt has no suite) and calls
    `promote_prompt` with it, so the whole gate + version flip runs inside
    the service's atomic path. A gate failure (`EvalGateFailure`) yields a
    terminal `blocked` status and audit `result="blocked"` with the failing
    score, leaving the active version untouched. Any other error yields
    `failed` / `result="error"`. Success yields `succeeded` /
    `result="success"`. Passes `redis` to `promote_prompt` so the exact-cache
    is invalidated on a successful flip.
    """
    await update_job(redis, job_id, status="running")
    gate = make_eval_gate(
        provider=provider,
        generate_model=generate_model,
        judge_model=judge_model,
        max_tokens=max_tokens,
    )
    try:
        async with session_factory() as session:
            promoted = await promote_prompt(
                prompt_name, version_num, session, redis=redis, gate=gate
            )
            await record_audit_event(
                session,
                actor_account_id=actor_account_id,
                actor_label=actor_label,
                action="prompt.promote",
                entity_type="prompt",
                entity_ref=prompt_name,
                version_num=version_num,
                result="success",
                details={"to_version": promoted.version_num},
            )
        await update_job(
            redis, job_id, status="succeeded", result={"passed": True}
        )
    except EvalGateFailure as exc:
        async with session_factory() as session:
            await record_audit_event(
                session,
                actor_account_id=actor_account_id,
                actor_label=actor_label,
                action="prompt.promote",
                entity_type="prompt",
                entity_ref=prompt_name,
                version_num=version_num,
                result="blocked",
                details={"score": exc.eval_run.score, "run_id": exc.eval_run.id},
            )
        await update_job(
            redis,
            job_id,
            status="blocked",
            result={"score": exc.eval_run.score, "passed": False},
        )
    except Exception as exc:  # noqa: BLE001 - terminal outcome is recorded, not swallowed
        async with session_factory() as session:
            await record_audit_event(
                session,
                actor_account_id=actor_account_id,
                actor_label=actor_label,
                action="prompt.promote",
                entity_type="prompt",
                entity_ref=prompt_name,
                version_num=version_num,
                result="error",
                details={"error": str(exc)},
            )
        await update_job(redis, job_id, status="failed", error=str(exc))
```

- [ ] **Step 4: Add the promote route to `dashboard.py`**

```python
class PromoteRequest(BaseModel):
    """Request body for promoting a prompt version (async, eval-gated)."""

    version_num: int


@router.post("/prompts/{name}/promote", response_model=JobCreatedResponse)
async def promote_route(
    name: str,
    body: PromoteRequest,
    redis: Redis = Depends(_get_redis),
    provider: AnthropicProvider = Depends(_get_eval_provider),
    operator: Account = Depends(require_operator),
) -> JobCreatedResponse:
    """Kick off an eval-gated promotion as a background job. Operator only.

    Returns a job id immediately; the UI polls `GET /prompts/jobs/{id}`. The
    background task runs the eval gate then the version flip, records the
    `prompt.promote` audit event (success/blocked/error), and invalidates the
    prompt's caches on success.
    """
    settings = get_settings()
    job_id = await promptjobs.create_job(
        redis, kind="promote", prompt_name=name, version_num=body.version_num
    )
    promptjobs.spawn(
        promptjobs.run_promote_job(
            job_id,
            prompt_name=name,
            version_num=body.version_num,
            provider=provider,
            generate_model=settings.default_model,
            judge_model=settings.eval_judge_model,
            max_tokens=settings.default_max_tokens,
            actor_account_id=operator.id,
            actor_label=operator.name,
            redis=redis,
            session_factory=SessionLocal,
        )
    )
    return JobCreatedResponse(job_id=job_id)
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_promptjobs.py tests/test_dashboard.py -k "promote" -v`
Expected: PASS.

- [ ] **Step 6: Run the whole backend suite + ruff**

Run: `ruff check gatekeep tests && ruff format --check gatekeep tests && pytest tests/test_dashboard.py tests/test_promptjobs.py tests/test_audit.py -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add gatekeep/promptjobs.py gatekeep/api/dashboard.py tests/test_promptjobs.py tests/test_dashboard.py
git commit -m "feat(dashboard): add async eval-gated promote job endpoint"
```

---

## Task 12: Frontend types

**Files:**
- Modify: `dashboard/src/api/types.ts`

**Interfaces:**
- Produces the TS interfaces mirroring the new backend response models. Later frontend tasks consume these exact names.

- [ ] **Step 1: Add the new types**

Append to `dashboard/src/api/types.ts`. Note `PromptVersionOut` already exists (Task 3 added `template` on the backend) - add `template` to it:

```typescript
/** A single version in a prompt's edit/promotion history. */
export interface PromptVersionOut {
  version_num: number;
  active: boolean;
  template: string;
  created_at: string;
  created_by: string | null;
  notes: string | null;
}
```

(Replace the existing `PromptVersionOut` interface with the one above - it gains `template`.)

Then append:

```typescript
/** An eval suite bound to a prompt. */
export interface SuiteOut {
  id: number;
  name: string;
  prompt_name: string;
  pass_threshold: number;
  created_at: string;
}

/** One eval case in a suite (reviewed or curated-and-unreviewed). */
export interface EvalCaseOut {
  id: number;
  check_type: string;
  expected: string | null;
  judge_criteria: string | null;
  reviewed: boolean;
  source: string;
  account_id: number | null;
  created_at: string;
  input_messages: Array<Record<string, unknown>>;
}

/** A prompt's eval suite and its cases (null suite => none registered). */
export interface PromptSuiteResponse {
  suite: SuiteOut | null;
  cases: EvalCaseOut[];
}

/** A prompt's unreviewed curated cases. */
export interface CurationResponse {
  cases: EvalCaseOut[];
}

/** The prompt name and version number a mutation produced/left active. */
export interface PromptMutationResponse {
  name: string;
  version_num: number;
}

/** A prompt's current A/B candidate config (nulls when none). */
export interface CandidateResponse {
  name: string;
  candidate_version_num: number | null;
  traffic_pct: number | null;
}

/** One audit-log row for the read-only feed. */
export interface AuditEventOut {
  id: number;
  created_at: string;
  actor_account_id: number | null;
  actor_label: string;
  action: string;
  entity_type: string;
  entity_ref: string | null;
  version_num: number | null;
  result: string;
  details: Record<string, unknown>;
}

/** A page of audit events, newest first. */
export interface AuditFeedResponse {
  events: AuditEventOut[];
}

/** A background job's per-case progress. */
export interface JobProgress {
  done: number;
  total: number;
}

/** A completed eval/promote job's outcome payload. */
export interface JobResult {
  score?: number | null;
  passed?: boolean | null;
}

/** Terminal and in-flight states of a background job. */
export type JobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "blocked";

/** Poll response for one background job. */
export interface JobStatusResponse {
  id: string;
  kind: string;
  prompt_name: string;
  version_num: number | null;
  status: JobStatus;
  progress: JobProgress;
  result: JobResult | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

/** The id of a background job the caller should poll. */
export interface JobCreatedResponse {
  job_id: string;
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/api/types.ts
git commit -m "feat(dashboard): add prompt-ops API types"
```

---

## Task 13: Frontend client functions

**Files:**
- Modify: `dashboard/src/api/client.ts`
- Test: `dashboard/src/api/client.test.ts`

**Interfaces:**
- Consumes: types from Task 12; existing `request`/`mutate` helpers.
- Produces client fns: `getPromptSuite`, `getPromptCuration`, `getAuditFeed`, `getJob`, `createPrompt`, `addPromptVersion`, `promotePrompt`, `rollbackPrompt`, `setCandidate`, `clearCandidate`, `createSuite`, `addCase`, `runEval`, `mineCuration`, `reviewCase`. `mutate` gains `"PUT" | "DELETE"` support.

- [ ] **Step 1: Write the failing test**

Add to `dashboard/src/api/client.test.ts` (follow the file's existing `fakeResponse` + `vi.stubGlobal("fetch", ...)` pattern):

```typescript
import {
  createPrompt,
  promotePrompt,
  setCandidate,
  clearCandidate,
  getJob,
} from "./client";

describe("prompt-ops client fns", () => {
  beforeEach(() => {
    const created = addIdentity({
      key: "sk-op",
      accountId: 1,
      accountName: "Op",
      isOperator: true,
    });
    setActiveIdentity(created.id);
  });

  it("POSTs a prompt create body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { name: "p", version_num: 1 }));
    vi.stubGlobal("fetch", fetchMock);

    const res = await createPrompt({ name: "p", template: "t" });
    expect(res.version_num).toBe(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/dashboard/api/prompts");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ name: "p", template: "t" });
  });

  it("returns a job id from promote", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(fakeResponse(200, { job_id: "abc" }));
    vi.stubGlobal("fetch", fetchMock);
    const res = await promotePrompt("p", 2);
    expect(res.job_id).toBe("abc");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });

  it("PUTs and DELETEs the candidate", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        fakeResponse(200, {
          name: "p",
          candidate_version_num: 2,
          traffic_pct: 10,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    await setCandidate("p", { version_num: 2, traffic_pct: 10 });
    expect(fetchMock.mock.calls[0][1].method).toBe("PUT");
    await clearCandidate("p");
    expect(fetchMock.mock.calls[1][1].method).toBe("DELETE");
  });

  it("GETs a job status", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        fakeResponse(200, {
          id: "j",
          kind: "eval_run",
          prompt_name: "p",
          version_num: null,
          status: "running",
          progress: { done: 1, total: 3 },
          result: null,
          error: null,
          created_at: "",
          updated_at: "",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const res = await getJob("j");
    expect(res.status).toBe("running");
  });
});
```

- [ ] **Step 2: Run to verify fail**

Run: `cd dashboard && npx vitest run src/api/client.test.ts`
Expected: FAIL (functions not exported).

- [ ] **Step 3: Widen `mutate` and add the client fns**

Change the `mutate` signature's method type in `client.ts`:

```typescript
async function mutate<T>(
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
```

Update the top-of-file type import to add the new types, then append the fns:

```typescript
import type {
  // ...existing imports...
  AuditFeedResponse,
  CandidateResponse,
  CurationResponse,
  EvalCaseOut,
  JobCreatedResponse,
  JobStatusResponse,
  PromptMutationResponse,
  PromptSuiteResponse,
} from "./types";
```

```typescript
/** Fetches a prompt's eval suite and cases (null suite => none registered). */
export function getPromptSuite(name: string): Promise<PromptSuiteResponse> {
  return request<PromptSuiteResponse>(`prompts/${encodeURIComponent(name)}/suite`);
}

/** Fetches a prompt's unreviewed curated cases. */
export function getPromptCuration(name: string): Promise<CurationResponse> {
  return request<CurationResponse>(`prompts/${encodeURIComponent(name)}/curation`);
}

/** Fetches the audit feed, filterable by entity/action. */
export function getAuditFeed(params?: {
  entityType?: string;
  entityRef?: string;
  action?: string;
  limit?: number;
}): Promise<AuditFeedResponse> {
  return request<AuditFeedResponse>("audit", {
    entity_type: params?.entityType,
    entity_ref: params?.entityRef,
    action: params?.action,
    limit: params?.limit,
  });
}

/** Polls one background job's status. */
export function getJob(jobId: string): Promise<JobStatusResponse> {
  return request<JobStatusResponse>(`prompts/jobs/${encodeURIComponent(jobId)}`);
}

/** Creates a prompt (initial active version 1). */
export function createPrompt(body: {
  name: string;
  template: string;
  notes?: string;
}): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>("POST", "prompts", body);
}

/** Appends a new inactive version to a prompt. */
export function addPromptVersion(
  name: string,
  body: { template: string; notes?: string },
): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/versions`,
    body,
  );
}

/** Kicks off an eval-gated promotion; returns a job id to poll. */
export function promotePrompt(
  name: string,
  versionNum: number,
): Promise<JobCreatedResponse> {
  return mutate<JobCreatedResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/promote`,
    { version_num: versionNum },
  );
}

/** Rolls a prompt back to its previously-active version. */
export function rollbackPrompt(name: string): Promise<PromptMutationResponse> {
  return mutate<PromptMutationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/rollback`,
  );
}

/** Sets/adjusts a prompt's A/B candidate version + traffic split. */
export function setCandidate(
  name: string,
  body: { version_num: number; traffic_pct: number },
): Promise<CandidateResponse> {
  return mutate<CandidateResponse>(
    "PUT",
    `prompts/${encodeURIComponent(name)}/candidate`,
    body,
  );
}

/** Clears a prompt's A/B candidate (100% back to active). */
export function clearCandidate(name: string): Promise<CandidateResponse> {
  return mutate<CandidateResponse>(
    "DELETE",
    `prompts/${encodeURIComponent(name)}/candidate`,
  );
}

/** Creates an eval suite for a prompt (threshold defaults server-side). */
export function createSuite(
  name: string,
  body: { threshold?: number },
): Promise<SuiteOut> {
  return mutate<SuiteOut>(
    "POST",
    `prompts/${encodeURIComponent(name)}/suite`,
    body,
  );
}

/** Adds a reviewed manual eval case to a prompt's suite. */
export function addCase(
  name: string,
  body: {
    input_messages: Array<Record<string, unknown>>;
    check_type: string;
    expected?: string;
    judge_criteria?: string;
  },
): Promise<EvalCaseOut> {
  return mutate<EvalCaseOut>(
    "POST",
    `prompts/${encodeURIComponent(name)}/suite/cases`,
    body,
  );
}

/** Kicks off an on-demand eval run; returns a job id to poll. */
export function runEval(
  name: string,
  body: { version_num?: number; model?: string },
): Promise<JobCreatedResponse> {
  return mutate<JobCreatedResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/eval-run`,
    body,
  );
}

/** Mines recent samples into unreviewed curated cases. */
export function mineCuration(
  name: string,
  body: { limit?: number },
): Promise<CurationResponse> {
  return mutate<CurationResponse>(
    "POST",
    `prompts/${encodeURIComponent(name)}/curation/mine`,
    body,
  );
}

/** Approves or rejects one curated case. */
export function reviewCase(
  name: string,
  caseId: number,
  approved: boolean,
): Promise<{ status: string }> {
  return mutate<{ status: string }>(
    "POST",
    `prompts/${encodeURIComponent(name)}/curation/${caseId}/review`,
    { approved },
  );
}
```

Add `SuiteOut` to the type import block as well.

- [ ] **Step 4: Run to verify pass**

Run: `cd dashboard && npx vitest run src/api/client.test.ts && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/api/client.ts dashboard/src/api/client.test.ts
git commit -m "feat(dashboard): add prompt-ops client functions"
```

---

## Task 14: `useJob` polling hook

**Files:**
- Create: `dashboard/src/hooks/useJob.ts`
- Test: `dashboard/src/hooks/useJob.test.ts`

**Interfaces:**
- Consumes: `getJob` (Task 13), `JobStatusResponse`.
- Produces: `useJob(jobId: string | null, opts?: { onSettled?: (job: JobStatusResponse) => void }) -> { job: JobStatusResponse | null; error: string | null }`. Polls every ~1s while `queued`/`running`; stops on a terminal status; treats a 404 (expired) as `error = "status unavailable, refresh"`.

- [ ] **Step 1: Write the failing test**

Create `dashboard/src/hooks/useJob.test.ts`:

```typescript
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { useJob } from "./useJob";
import * as client from "../api/client";
import type { JobStatusResponse } from "../api/types";

function job(status: JobStatusResponse["status"]): JobStatusResponse {
  return {
    id: "j",
    kind: "eval_run",
    prompt_name: "p",
    version_num: null,
    status,
    progress: { done: 0, total: 0 },
    result: null,
    error: null,
    created_at: "",
    updated_at: "",
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useJob", () => {
  it("polls to a terminal status and calls onSettled once", async () => {
    const spy = vi
      .spyOn(client, "getJob")
      .mockResolvedValueOnce(job("running"))
      .mockResolvedValueOnce(job("succeeded"));
    const onSettled = vi.fn();
    const { result } = renderHook(() => useJob("j", { onSettled }));

    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() => expect(result.current.job?.status).toBe("running"));
    await vi.advanceTimersByTimeAsync(1000);
    await waitFor(() => expect(result.current.job?.status).toBe("succeeded"));
    expect(onSettled).toHaveBeenCalledTimes(1);
    // No further polls after terminal.
    await vi.advanceTimersByTimeAsync(3000);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("surfaces an expired job as an error", async () => {
    vi.spyOn(client, "getJob").mockRejectedValue(new Error("job not found or expired"));
    const { result } = renderHook(() => useJob("j"));
    await vi.advanceTimersByTimeAsync(0);
    await waitFor(() =>
      expect(result.current.error).toBe("status unavailable, refresh"),
    );
  });

  it("is idle when jobId is null", async () => {
    const spy = vi.spyOn(client, "getJob");
    const { result } = renderHook(() => useJob(null));
    await vi.advanceTimersByTimeAsync(2000);
    expect(spy).not.toHaveBeenCalled();
    expect(result.current.job).toBeNull();
  });
});
```

If `@testing-library/react` is not yet a dev dependency, install it first: `cd dashboard && npm install -D @testing-library/react`. Verify by checking `dashboard/package.json` `devDependencies`.

- [ ] **Step 2: Run to verify fail**

Run: `cd dashboard && npx vitest run src/hooks/useJob.test.ts`
Expected: FAIL (`useJob` not defined).

- [ ] **Step 3: Implement `useJob.ts`**

```typescript
import { useEffect, useRef, useState } from "react";
import { getJob } from "../api/client";
import type { JobStatusResponse } from "../api/types";

const TERMINAL: ReadonlySet<string> = new Set(["succeeded", "failed", "blocked"]);
const POLL_INTERVAL_MS = 1000;

/**
 * Polls a background job's status until it reaches a terminal state.
 *
 * While `jobId` names a job in `queued`/`running`, this re-fetches
 * `GET /prompts/jobs/{id}` every second and returns the latest snapshot.
 * Polling stops as soon as the status is terminal (`succeeded`, `failed`,
 * `blocked`), at which point `onSettled` fires exactly once. A rejected
 * fetch (e.g. the job TTL lapsed => 404) stops polling and surfaces
 * `error = "status unavailable, refresh"`. Passing `jobId === null` leaves
 * the hook idle (no polling, `job === null`).
 *
 * @param jobId - The job to poll, or null to stay idle.
 * @param opts.onSettled - Called once with the terminal job snapshot, e.g.
 *   to refetch the panel the job mutated.
 * @returns The latest job snapshot (or null) and an error message (or null).
 */
export function useJob(
  jobId: string | null,
  opts?: { onSettled?: (job: JobStatusResponse) => void },
): { job: JobStatusResponse | null; error: string | null } {
  const [job, setJob] = useState<JobStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keep the latest onSettled without making it a polling dependency.
  const onSettledRef = useRef(opts?.onSettled);
  onSettledRef.current = opts?.onSettled;

  useEffect(() => {
    if (!jobId) {
      setJob(null);
      setError(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const snapshot = await getJob(jobId);
        if (cancelled) return;
        setJob(snapshot);
        if (TERMINAL.has(snapshot.status)) {
          onSettledRef.current?.(snapshot);
          return; // stop scheduling
        }
        timer = setTimeout(poll, POLL_INTERVAL_MS);
      } catch {
        if (cancelled) return;
        setError("status unavailable, refresh");
      }
    };

    setError(null);
    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  return { job, error };
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd dashboard && npx vitest run src/hooks/useJob.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/hooks/useJob.ts dashboard/src/hooks/useJob.test.ts dashboard/package.json dashboard/package-lock.json
git commit -m "feat(dashboard): add useJob polling hook"
```

---

## Task 15: Prompts tab wiring (Header + App + page shell)

**Files:**
- Modify: `dashboard/src/components/Header.tsx`
- Modify: `dashboard/src/App.tsx`
- Create: `dashboard/src/pages/PromptsPage.tsx`

**Interfaces:**
- Consumes: `getPrompts` (existing), `MeResponse`, `PromptOut`.
- Produces: `TabKey` gains `"prompts"`; `PromptsPage` is rendered for that tab; non-operators see a "requires operator" notice.

- [ ] **Step 1: Add the tab to `Header.tsx`**

Change the type and add the button:

```typescript
export type TabKey = "analytics" | "management" | "prompts";
```

In the `<nav>`, add after the Accounts button:

```tsx
          <button className={tabClass("prompts")} onClick={() => onTabChange("prompts")}>
            Prompts
          </button>
```

- [ ] **Step 2: Route the tab in `App.tsx`**

Import the page and extend the render branch. Replace the ternary with:

```tsx
      {tab === "analytics" ? (
        <DashboardPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
        />
      ) : tab === "prompts" ? (
        <PromptsPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
        />
      ) : (
        <ManagementPage
          me={me}
          meError={meError}
          onRetryMe={loadMe}
          onUnauthorized={handleUnauthorized}
          onMeChanged={setMe}
        />
      )}
```

Add the import: `import PromptsPage from "./pages/PromptsPage";`

- [ ] **Step 3: Create the `PromptsPage` shell (master-detail + operator gate)**

```tsx
import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getPrompts } from "../api/client";
import { useApiErrorHandler } from "../hooks/useApiErrorHandler";
import type { MeResponse, PromptOut } from "../api/types";
import PromptDetail from "../components/PromptDetail";
import CreatePromptModal from "../components/prompts/CreatePromptModal";
import AuditFeed from "../components/prompts/AuditFeed";

interface PromptsPageProps {
  me: MeResponse | null;
  meError: string | null;
  onRetryMe: () => void;
  onUnauthorized: () => void;
}

/**
 * Prompts tab: master-detail operator console for the full prompt lifecycle.
 * Left pane lists prompts; the right pane shows the selected prompt's
 * versions, A/B candidate, evals, and curation, plus a global audit feed.
 * Gated to operators, matching the Accounts tab; non-operators see a notice.
 */
export default function PromptsPage({
  me,
  meError,
  onRetryMe,
  onUnauthorized,
}: PromptsPageProps) {
  const [prompts, setPrompts] = useState<PromptOut[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const { error, setError, handleError } = useApiErrorHandler(onUnauthorized);

  const loadPrompts = useCallback(async () => {
    setError(null);
    try {
      const res = await getPrompts();
      setPrompts(res.prompts);
      setSelected((cur) => cur ?? res.prompts[0]?.name ?? null);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      handleError(err, "Failed to load prompts");
    }
  }, [setError, handleError, onUnauthorized]);

  useEffect(() => {
    if (me?.is_operator) loadPrompts();
  }, [me?.is_operator, loadPrompts]);

  if (!me) {
    if (meError) {
      return (
        <div className="mx-6 mt-6 flex items-center justify-between rounded-lg border border-red-900 bg-red-950/50 px-4 py-3 text-sm text-red-300">
          <span>{meError}</span>
          <button
            onClick={onRetryMe}
            className="rounded border border-red-800 px-3 py-1 text-xs text-red-200 hover:bg-red-900"
          >
            Retry
          </button>
        </div>
      );
    }
    return <p className="mx-6 mt-6 text-sm text-slate-400">Loading account...</p>;
  }

  if (!me.is_operator) {
    return (
      <p className="mx-6 mt-6 text-sm text-slate-400">
        The Prompts console requires operator access.
      </p>
    );
  }

  return (
    <div className="flex gap-4 px-6 py-6">
      <aside className="w-64 shrink-0">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium text-slate-300">Prompts</h2>
          <button
            onClick={() => setCreating(true)}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            New prompt
          </button>
        </div>
        {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
        <ul className="space-y-1">
          {prompts.map((p) => (
            <li key={p.name}>
              <button
                onClick={() => setSelected(p.name)}
                className={`w-full rounded px-2 py-1.5 text-left text-sm ${
                  selected === p.name
                    ? "bg-slate-800 text-slate-100"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                <span className="block truncate">{p.name}</span>
                <span className="text-xs text-slate-500">
                  {p.active_version_num ? `v${p.active_version_num}` : "no active version"}
                </span>
              </button>
            </li>
          ))}
          {prompts.length === 0 && (
            <li className="text-sm text-slate-500">No prompts registered.</li>
          )}
        </ul>
      </aside>
      <section className="min-w-0 flex-1 space-y-4">
        {selected ? (
          <PromptDetail
            key={selected}
            name={selected}
            onUnauthorized={onUnauthorized}
            onPromptsChanged={loadPrompts}
          />
        ) : (
          <p className="text-sm text-slate-500">Select a prompt to view its details.</p>
        )}
        <AuditFeed onUnauthorized={onUnauthorized} />
      </section>
      {creating && (
        <CreatePromptModal
          onClose={() => setCreating(false)}
          onCreated={(name) => {
            setCreating(false);
            setSelected(name);
            loadPrompts();
          }}
          onUnauthorized={onUnauthorized}
        />
      )}
    </div>
  );
}
```

This references `PromptDetail`, `CreatePromptModal`, and `AuditFeed`, created in later tasks. To keep the tree compiling between tasks, create minimal placeholder files now, each replaced in its own task.

- [ ] **Step 4: Create minimal placeholders so the app compiles**

Create `dashboard/src/components/prompts/CreatePromptModal.tsx`:

```tsx
/** Placeholder; implemented in the version-management task. */
export default function CreatePromptModal(_props: {
  onClose: () => void;
  onCreated: (name: string) => void;
  onUnauthorized: () => void;
}) {
  return null;
}
```

Create `dashboard/src/components/PromptDetail.tsx`:

```tsx
/** Placeholder; implemented in later tasks. */
export default function PromptDetail(_props: {
  name: string;
  onUnauthorized: () => void;
  onPromptsChanged: () => void;
}) {
  return null;
}
```

Create `dashboard/src/components/prompts/AuditFeed.tsx`:

```tsx
/** Placeholder; implemented in the audit-feed task. */
export default function AuditFeed(_props: { onUnauthorized: () => void; entityRef?: string }) {
  return null;
}
```

- [ ] **Step 5: Typecheck + build**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/components/Header.tsx dashboard/src/App.tsx dashboard/src/pages/PromptsPage.tsx dashboard/src/components/PromptDetail.tsx dashboard/src/components/prompts/
git commit -m "feat(dashboard): add Prompts tab shell and routing"
```

---

## Task 16: `CreatePromptModal` + `PromptDetail` container + Versions section

**Files:**
- Rewrite: `dashboard/src/components/prompts/CreatePromptModal.tsx`
- Rewrite: `dashboard/src/components/PromptDetail.tsx`
- Create: `dashboard/src/components/prompts/VersionsSection.tsx`

**Interfaces:**
- Consumes: `getPromptVersions`, `createPrompt`, `addPromptVersion`, `promotePrompt`, `rollbackPrompt`, `useJob`, types `PromptVersionOut`.
- Produces: `PromptDetail` fetches `getPromptVersions(name)` and renders `VersionsSection` (+ later Candidate/Evals/Curation). `VersionsSection` handles view/diff, add-version, promote (job), rollback with a confirm modal.

- [ ] **Step 1: Implement `CreatePromptModal`** (model the existing `CreateAccountModal` / `CreateKeyModal` styling)

```tsx
import { useState } from "react";
import { UnauthorizedError, createPrompt } from "../../api/client";

interface CreatePromptModalProps {
  onClose: () => void;
  onCreated: (name: string) => void;
  onUnauthorized: () => void;
}

/** Modal to create a new prompt with its initial (active) version 1. */
export default function CreatePromptModal({
  onClose,
  onCreated,
  onUnauthorized,
}: CreatePromptModalProps) {
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    setBusy(true);
    try {
      const res = await createPrompt({
        name: name.trim(),
        template,
        notes: notes.trim() || undefined,
      });
      onCreated(res.name);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to create prompt");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/50 p-4">
      <div className="w-full max-w-lg rounded-lg border border-slate-800 bg-slate-900 p-5">
        <h3 className="mb-3 text-sm font-medium text-slate-200">New prompt</h3>
        {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
        <label className="mb-1 block text-xs text-slate-400">Name</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        />
        <label className="mb-1 block text-xs text-slate-400">Template</label>
        <textarea
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          rows={6}
          className="mb-3 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-sm text-slate-200"
        />
        <label className="mb-1 block text-xs text-slate-400">Notes (optional)</label>
        <input
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={busy || !name.trim() || !template}
            className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Implement `PromptDetail`** (container that loads versions/suite/candidate and renders sub-sections)

```tsx
import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  getPromptVersions,
} from "../api/client";
import type { PromptVersionOut } from "../api/types";
import VersionsSection from "./prompts/VersionsSection";
import CandidateSection from "./prompts/CandidateSection";
import EvalsSection from "./prompts/EvalsSection";
import CurationSection from "./prompts/CurationSection";

interface PromptDetailProps {
  name: string;
  onUnauthorized: () => void;
  /** Refresh the master list (e.g. after a promote changes active version). */
  onPromptsChanged: () => void;
}

/**
 * Right-pane container for a selected prompt: loads its version timeline and
 * fans out to the Versions, A/B candidate, Evals, and Curation sub-sections.
 */
export default function PromptDetail({
  name,
  onUnauthorized,
  onPromptsChanged,
}: PromptDetailProps) {
  const [versions, setVersions] = useState<PromptVersionOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const loadVersions = useCallback(async () => {
    setError(null);
    try {
      const res = await getPromptVersions(name);
      setVersions(res.versions);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load versions");
    }
  }, [name, onUnauthorized]);

  useEffect(() => {
    loadVersions();
  }, [loadVersions]);

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold text-slate-100">{name}</h2>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <VersionsSection
        name={name}
        versions={versions}
        onChanged={() => {
          loadVersions();
          onPromptsChanged();
        }}
        onUnauthorized={onUnauthorized}
      />
      <CandidateSection
        name={name}
        versions={versions}
        onUnauthorized={onUnauthorized}
      />
      <EvalsSection name={name} versions={versions} onUnauthorized={onUnauthorized} />
      <CurationSection name={name} onUnauthorized={onUnauthorized} />
    </div>
  );
}
```

Create minimal placeholders for `CandidateSection`, `EvalsSection`, `CurationSection` now (each replaced in its own task):

```tsx
// dashboard/src/components/prompts/CandidateSection.tsx
import type { PromptVersionOut } from "../../api/types";
/** Placeholder; implemented in the A/B candidate task. */
export default function CandidateSection(_props: {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}) {
  return null;
}
```

```tsx
// dashboard/src/components/prompts/EvalsSection.tsx
import type { PromptVersionOut } from "../../api/types";
/** Placeholder; implemented in the evals task. */
export default function EvalsSection(_props: {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}) {
  return null;
}
```

```tsx
// dashboard/src/components/prompts/CurationSection.tsx
/** Placeholder; implemented in the curation task. */
export default function CurationSection(_props: {
  name: string;
  onUnauthorized: () => void;
}) {
  return null;
}
```

- [ ] **Step 3: Implement `VersionsSection`** (timeline + add version + promote/rollback with a confirm + job UX)

```tsx
import { useState } from "react";
import {
  UnauthorizedError,
  addPromptVersion,
  promotePrompt,
  rollbackPrompt,
} from "../../api/client";
import { useJob } from "../../hooks/useJob";
import type { PromptVersionOut } from "../../api/types";

interface VersionsSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onChanged: () => void;
  onUnauthorized: () => void;
}

/**
 * Versions sub-section: the prompt's version timeline (with template text),
 * an "Add version" editor, and per-version Promote / Rollback actions behind
 * a confirm. Promote runs as a background job (eval gate + cache
 * invalidation), surfaced inline via useJob.
 */
export default function VersionsSection({
  name,
  versions,
  onChanged,
  onUnauthorized,
}: VersionsSectionProps) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<
    { kind: "promote" | "rollback"; versionNum: number } | null
  >(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const { job, error: jobError } = useJob(jobId, {
    onSettled: () => {
      setJobId(null);
      onChanged();
    },
  });

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const submitAdd = async () => {
    setError(null);
    try {
      await addPromptVersion(name, { template: draft, notes: notes.trim() || undefined });
      setAdding(false);
      setDraft("");
      setNotes("");
      onChanged();
    } catch (err) {
      fail(err, "Failed to add version");
    }
  };

  const runConfirmed = async () => {
    if (!confirm) return;
    setError(null);
    try {
      if (confirm.kind === "promote") {
        const res = await promotePrompt(name, confirm.versionNum);
        setJobId(res.job_id);
      } else {
        await rollbackPrompt(name);
        onChanged();
      }
    } catch (err) {
      fail(err, `Failed to ${confirm.kind}`);
    } finally {
      setConfirm(null);
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Versions</h3>
        <button
          onClick={() => setAdding((v) => !v)}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
        >
          {adding ? "Cancel" : "Add version"}
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}

      {adding && (
        <div className="mb-4 space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={6}
            placeholder="New template text"
            className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-sm text-slate-200"
          />
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notes (optional)"
            className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
          <button
            onClick={submitAdd}
            disabled={!draft}
            className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            Save version
          </button>
        </div>
      )}

      {jobId && (
        <p className="mb-2 text-xs text-amber-300">
          Promotion job {job?.status ?? "queued"}
          {job?.progress.total ? ` (${job.progress.done}/${job.progress.total})` : ""}...
        </p>
      )}
      {jobError && <p className="mb-2 text-xs text-red-400">{jobError}</p>}
      {job?.status === "blocked" && (
        <p className="mb-2 text-xs text-red-400">
          Promotion blocked by eval gate, score {job.result?.score?.toFixed(2)}.
        </p>
      )}

      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Version</th>
            <th className="pb-2">Status</th>
            <th className="pb-2">Created by</th>
            <th className="pb-2">Template</th>
            <th className="pb-2 text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.version_num} className="border-t border-slate-800 align-top">
              <td className="py-2 text-slate-200">v{v.version_num}</td>
              <td className="py-2">
                {v.active ? (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Active
                  </span>
                ) : (
                  "-"
                )}
              </td>
              <td className="py-2 text-slate-300">{v.created_by ?? "-"}</td>
              <td className="max-w-md py-2">
                <pre className="max-h-24 overflow-auto whitespace-pre-wrap font-mono text-xs text-slate-400">
                  {v.template}
                </pre>
              </td>
              <td className="py-2 text-right">
                {!v.active && (
                  <button
                    onClick={() => setConfirm({ kind: "promote", versionNum: v.version_num })}
                    className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
                  >
                    Promote
                  </button>
                )}
              </td>
            </tr>
          ))}
          {versions.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                No versions yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="mt-3">
        <button
          onClick={() => setConfirm({ kind: "rollback", versionNum: 0 })}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Roll back to previous
        </button>
      </div>

      {confirm && (
        <div className="fixed inset-0 z-10 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 p-5">
            <p className="mb-4 text-sm text-slate-200">
              {confirm.kind === "promote"
                ? `Promote v${confirm.versionNum}? This runs the eval gate (if a suite exists) and invalidates this prompt's cache.`
                : "Roll back to the previously-active version? This invalidates this prompt's cache."}
            </p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirm(null)}
                className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                onClick={runConfirmed}
                className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Typecheck**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/PromptDetail.tsx dashboard/src/components/prompts/
git commit -m "feat(dashboard): add prompt versions section and create modal"
```

---

## Task 17: A/B Candidate section

**Files:**
- Rewrite: `dashboard/src/components/prompts/CandidateSection.tsx`

**Interfaces:**
- Consumes: `setCandidate`, `clearCandidate`, `getPromptVersions` (for current candidate) - actually read the candidate from a dedicated fetch. Since `getPrompts`/versions don't return the candidate, fetch it via `setCandidate`'s response after a change; for the initial display, add a small read. To avoid a new endpoint, display current config from the last mutation and a "not loaded" default, OR reuse `CandidateResponse`. Simplest: on mount, we have no read route for the candidate alone. Use the `PUT`/`DELETE` responses to reflect state, and show "unknown until you set/clear" initially.

Decision: the design's `GET /prompts` does not include candidate. Rather than add a route, the section lets the operator set a candidate (version dropdown + pct) and clear it, reflecting the server's `CandidateResponse` after each action. This matches the spec's set/adjust/clear surface without a new read.

- [ ] **Step 1: Implement `CandidateSection`**

```tsx
import { useState } from "react";
import {
  UnauthorizedError,
  clearCandidate,
  setCandidate,
} from "../../api/client";
import type { CandidateResponse, PromptVersionOut } from "../../api/types";

interface CandidateSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}

/**
 * A/B candidate sub-section: set or adjust the candidate version + traffic
 * percentage, or clear it. Reflects the server's returned candidate config
 * after each action (setting a candidate never runs the eval gate or
 * invalidates cache).
 */
export default function CandidateSection({
  name,
  versions,
  onUnauthorized,
}: CandidateSectionProps) {
  const [versionNum, setVersionNum] = useState<number | "">("");
  const [pct, setPct] = useState<number>(10);
  const [current, setCurrent] = useState<CandidateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const apply = async () => {
    if (versionNum === "") return;
    setError(null);
    try {
      const res = await setCandidate(name, {
        version_num: Number(versionNum),
        traffic_pct: pct,
      });
      setCurrent(res);
    } catch (err) {
      fail(err, "Failed to set candidate");
    }
  };

  const clear = async () => {
    setError(null);
    try {
      const res = await clearCandidate(name);
      setCurrent(res);
    } catch (err) {
      fail(err, "Failed to clear candidate");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h3 className="mb-3 text-sm font-medium text-slate-300">A/B candidate</h3>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      {current && (
        <p className="mb-3 text-xs text-slate-400">
          {current.candidate_version_num === null
            ? "No candidate configured (100% active)."
            : `Candidate v${current.candidate_version_num} at ${current.traffic_pct}% traffic.`}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-400">
          Version
          <select
            value={versionNum}
            onChange={(e) => setVersionNum(e.target.value === "" ? "" : Number(e.target.value))}
            className="mt-1 block rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          >
            <option value="">Select...</option>
            {versions.map((v) => (
              <option key={v.version_num} value={v.version_num}>
                v{v.version_num}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Traffic %
          <input
            type="number"
            min={0}
            max={100}
            value={pct}
            onChange={(e) => setPct(Number(e.target.value))}
            className="mt-1 block w-20 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        </label>
        <button
          onClick={apply}
          disabled={versionNum === ""}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Set candidate
        </button>
        <button
          onClick={clear}
          className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
        >
          Clear
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/prompts/CandidateSection.tsx
git commit -m "feat(dashboard): add A/B candidate section"
```

---

## Task 18: Evals section (suite, cases, run eval job, history)

**Files:**
- Rewrite: `dashboard/src/components/prompts/EvalsSection.tsx`

**Interfaces:**
- Consumes: `getPromptSuite`, `createSuite`, `addCase`, `runEval`, `getEvalHistory` (existing), `useJob`; types `SuiteOut`, `EvalCaseOut`, `EvalRunOut`.

- [ ] **Step 1: Implement `EvalsSection`**

```tsx
import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  addCase,
  createSuite,
  getEvalHistory,
  getPromptSuite,
  runEval,
} from "../../api/client";
import { useJob } from "../../hooks/useJob";
import type {
  EvalCaseOut,
  EvalRunOut,
  PromptVersionOut,
  SuiteOut,
} from "../../api/types";

interface EvalsSectionProps {
  name: string;
  versions: PromptVersionOut[];
  onUnauthorized: () => void;
}

/**
 * Evals sub-section: shows the prompt's eval suite and cases (or offers to
 * create a suite), a "Run eval" action (background job with inline
 * progress), and the eval-run history for this prompt.
 */
export default function EvalsSection({
  name,
  versions,
  onUnauthorized,
}: EvalsSectionProps) {
  const [suite, setSuite] = useState<SuiteOut | null>(null);
  const [cases, setCases] = useState<EvalCaseOut[]>([]);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      const [suiteRes, historyRes] = await Promise.all([
        getPromptSuite(name),
        getEvalHistory(name),
      ]);
      setSuite(suiteRes.suite);
      setCases(suiteRes.cases);
      setRuns(historyRes.runs);
    } catch (err) {
      fail(err, "Failed to load evals");
    }
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [load]);

  const { job, error: jobError } = useJob(jobId, {
    onSettled: () => {
      setJobId(null);
      load();
    },
  });

  const create = async () => {
    try {
      await createSuite(name, {});
      load();
    } catch (err) {
      fail(err, "Failed to create suite");
    }
  };

  const run = async () => {
    try {
      const res = await runEval(name, {});
      setJobId(res.job_id);
    } catch (err) {
      fail(err, "Failed to start eval run");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Evals</h3>
        {suite ? (
          <button
            onClick={run}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            Run eval
          </button>
        ) : (
          <button
            onClick={create}
            className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800"
          >
            Create suite
          </button>
        )}
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}

      {suite ? (
        <>
          <p className="mb-2 text-xs text-slate-400">
            Threshold {suite.pass_threshold} - {cases.length} case(s)
          </p>
          {jobId && (
            <p className="mb-2 text-xs text-amber-300">
              Eval job {job?.status ?? "queued"}
              {job?.progress.total ? ` (${job.progress.done}/${job.progress.total})` : ""}...
            </p>
          )}
          {jobError && <p className="mb-2 text-xs text-red-400">{jobError}</p>}
          <table className="mb-4 w-full text-left text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wide text-slate-500">
                <th className="pb-2">When</th>
                <th className="pb-2">Version</th>
                <th className="pb-2 text-right">Score</th>
                <th className="pb-2">Result</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id} className="border-t border-slate-800">
                  <td className="py-2 text-slate-300">
                    {new Date(r.created_at).toLocaleString()}
                  </td>
                  <td className="py-2 text-slate-300">v{r.version_num}</td>
                  <td className="py-2 text-right text-slate-300">{r.score.toFixed(2)}</td>
                  <td className="py-2">
                    {r.passed ? (
                      <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                        Pass
                      </span>
                    ) : (
                      <span className="rounded bg-red-900 px-2 py-0.5 text-xs text-red-300">
                        Fail
                      </span>
                    )}
                  </td>
                </tr>
              ))}
              {runs.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-3 text-center text-slate-500">
                    No eval runs yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <AddCaseForm name={name} onAdded={load} onUnauthorized={onUnauthorized} />
        </>
      ) : (
        <p className="text-sm text-slate-500">No eval suite registered.</p>
      )}
    </div>
  );
}

/** Inline form to add a reviewed manual eval case (contains/exact/llm_judge). */
function AddCaseForm({
  name,
  onAdded,
  onUnauthorized,
}: {
  name: string;
  onAdded: () => void;
  onUnauthorized: () => void;
}) {
  const [content, setContent] = useState("");
  const [checkType, setCheckType] = useState("contains");
  const [expected, setExpected] = useState("");
  const [criteria, setCriteria] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    try {
      await addCase(name, {
        input_messages: [{ role: "user", content }],
        check_type: checkType,
        expected: checkType === "llm_judge" ? undefined : expected,
        judge_criteria: checkType === "llm_judge" ? criteria : undefined,
      });
      setContent("");
      setExpected("");
      setCriteria("");
      onAdded();
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to add case");
    }
  };

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <h4 className="text-xs font-medium text-slate-400">Add case</h4>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <input
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="User message"
        className="w-full rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
      />
      <div className="flex flex-wrap gap-2">
        <select
          value={checkType}
          onChange={(e) => setCheckType(e.target.value)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        >
          <option value="contains">contains</option>
          <option value="icontains">icontains</option>
          <option value="exact">exact</option>
          <option value="llm_judge">llm_judge</option>
        </select>
        {checkType === "llm_judge" ? (
          <input
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            placeholder="Judge criteria"
            className="flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        ) : (
          <input
            value={expected}
            onChange={(e) => setExpected(e.target.value)}
            placeholder="Expected"
            className="flex-1 rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
          />
        )}
        <button
          onClick={submit}
          disabled={!content}
          className="rounded bg-indigo-600 px-3 py-1.5 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
        >
          Add
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/prompts/EvalsSection.tsx
git commit -m "feat(dashboard): add evals section with run-eval job"
```

---

## Task 19: Curation section

**Files:**
- Rewrite: `dashboard/src/components/prompts/CurationSection.tsx`

**Interfaces:**
- Consumes: `getPromptCuration`, `mineCuration`, `reviewCase`; type `EvalCaseOut`.

- [ ] **Step 1: Implement `CurationSection`**

```tsx
import { useCallback, useEffect, useState } from "react";
import {
  UnauthorizedError,
  getPromptCuration,
  mineCuration,
  reviewCase,
} from "../../api/client";
import type { EvalCaseOut } from "../../api/types";

interface CurationSectionProps {
  name: string;
  onUnauthorized: () => void;
}

/**
 * Curation sub-section: "Mine samples" turns recent traffic into unreviewed
 * candidate cases; each is approved (kept, marked reviewed) or rejected
 * (deleted) inline.
 */
export default function CurationSection({ name, onUnauthorized }: CurationSectionProps) {
  const [cases, setCases] = useState<EvalCaseOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = (err: unknown, fallback: string) => {
    if (err instanceof UnauthorizedError) return onUnauthorized();
    setError(err instanceof Error ? err.message : fallback);
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getPromptCuration(name);
      setCases(res.cases);
    } catch (err) {
      fail(err, "Failed to load curation");
    }
  }, [name]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
  }, [load]);

  const mine = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await mineCuration(name, { limit: 20 });
      setCases(res.cases);
    } catch (err) {
      fail(err, "Failed to mine samples");
    } finally {
      setBusy(false);
    }
  };

  const review = async (caseId: number, approved: boolean) => {
    try {
      await reviewCase(name, caseId, approved);
      setCases((cur) => cur.filter((c) => c.id !== caseId));
    } catch (err) {
      fail(err, "Failed to review case");
    }
  };

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Curation</h3>
        <button
          onClick={mine}
          disabled={busy}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-200 hover:bg-slate-800 disabled:opacity-50"
        >
          {busy ? "Mining..." : "Mine samples"}
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      <ul className="space-y-2">
        {cases.map((c) => (
          <li key={c.id} className="rounded border border-slate-800 p-2">
            <pre className="mb-2 max-h-24 overflow-auto whitespace-pre-wrap font-mono text-xs text-slate-400">
              {JSON.stringify(c.input_messages, null, 2)}
            </pre>
            {c.judge_criteria && (
              <p className="mb-2 text-xs text-slate-400">Rubric: {c.judge_criteria}</p>
            )}
            <div className="flex gap-2">
              <button
                onClick={() => review(c.id, true)}
                className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600"
              >
                Approve
              </button>
              <button
                onClick={() => review(c.id, false)}
                className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
              >
                Reject
              </button>
            </div>
          </li>
        ))}
        {cases.length === 0 && (
          <li className="text-sm text-slate-500">No unreviewed curated cases.</li>
        )}
      </ul>
    </div>
  );
}
```

- [ ] **Step 2: Typecheck**

Run: `cd dashboard && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/prompts/CurationSection.tsx
git commit -m "feat(dashboard): add curation section"
```

---

## Task 20: Audit feed panel

**Files:**
- Rewrite: `dashboard/src/components/prompts/AuditFeed.tsx`

**Interfaces:**
- Consumes: `getAuditFeed`; type `AuditEventOut`. Props: `{ entityRef?: string; onUnauthorized: () => void }`. When `entityRef` is set, filters to that prompt; otherwise the global feed.

- [ ] **Step 1: Implement `AuditFeed`**

```tsx
import { useCallback, useEffect, useState } from "react";
import { UnauthorizedError, getAuditFeed } from "../../api/client";
import type { AuditEventOut } from "../../api/types";

interface AuditFeedProps {
  onUnauthorized: () => void;
  /** When set, scopes the feed to one prompt (entity_ref); else global. */
  entityRef?: string;
}

const RESULT_BADGE: Record<string, string> = {
  success: "bg-emerald-900 text-emerald-300",
  blocked: "bg-amber-900 text-amber-300",
  error: "bg-red-900 text-red-300",
};

/**
 * Read-only audit feed. Shows the newest mutating actions (actor, action,
 * target, result), optionally scoped to a single prompt via `entityRef`.
 */
export default function AuditFeed({ onUnauthorized, entityRef }: AuditFeedProps) {
  const [events, setEvents] = useState<AuditEventOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await getAuditFeed(
        entityRef ? { entityType: "prompt", entityRef } : { limit: 100 },
      );
      setEvents(res.events);
    } catch (err) {
      if (err instanceof UnauthorizedError) return onUnauthorized();
      setError(err instanceof Error ? err.message : "Failed to load audit feed");
    }
  }, [entityRef, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">
          Audit{entityRef ? ` - ${entityRef}` : ""}
        </h3>
        <button
          onClick={load}
          className="rounded border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:bg-slate-800"
        >
          Refresh
        </button>
      </div>
      {error && <p className="mb-2 text-xs text-red-400">{error}</p>}
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">When</th>
            <th className="pb-2">Actor</th>
            <th className="pb-2">Action</th>
            <th className="pb-2">Target</th>
            <th className="pb-2">Result</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id} className="border-t border-slate-800">
              <td className="py-2 text-slate-300">
                {new Date(e.created_at).toLocaleString()}
              </td>
              <td className="py-2 text-slate-300">{e.actor_label}</td>
              <td className="py-2 text-slate-200">{e.action}</td>
              <td className="py-2 text-slate-300">
                {e.entity_ref ?? "-"}
                {e.version_num !== null ? ` v${e.version_num}` : ""}
              </td>
              <td className="py-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs ${
                    RESULT_BADGE[e.result] ?? "bg-slate-800 text-slate-300"
                  }`}
                >
                  {e.result}
                </span>
              </td>
            </tr>
          ))}
          {events.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                No audit events yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Wire the per-prompt audit feed into `PromptDetail`**

In `PromptDetail.tsx`, add `import AuditFeed from "./prompts/AuditFeed";` and render it at the end of the returned `<div className="space-y-4">`, scoped to the prompt:

```tsx
      <CurationSection name={name} onUnauthorized={onUnauthorized} />
      <AuditFeed entityRef={name} onUnauthorized={onUnauthorized} />
```

(The `PromptsPage` already renders a global `<AuditFeed />` below the detail.)

- [ ] **Step 3: Typecheck + build**

Run: `cd dashboard && npx tsc --noEmit && npm run build`
Expected: no errors; build succeeds.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/prompts/AuditFeed.tsx dashboard/src/components/PromptDetail.tsx
git commit -m "feat(dashboard): add audit feed panel"
```

---

## Task 21: Move read-only prompt/eval panels out of Analytics

**Files:**
- Modify: `dashboard/src/pages/DashboardPage.tsx`
- Delete: `dashboard/src/components/PromptsPanel.tsx`, `dashboard/src/components/EvalHistoryPanel.tsx`

**Interfaces:**
- The spec says the existing read-only `PromptsPanel` / `EvalHistoryPanel` move to the Prompts tab. Their functionality is now covered by `VersionsSection` and `EvalsSection`, so remove them from Analytics and delete the now-unused components.

- [ ] **Step 1: Remove the panels from `DashboardPage.tsx`**

- Delete the imports of `PromptsPanel` and `EvalHistoryPanel`.
- Delete the `getEvalHistory` / `getPrompts` imports (from the `client` import block) if unused elsewhere in the file, plus the `EvalRunOut` / `PromptOut` type imports.
- Delete the `runs`/`prompts` state, the `loadOperatorData` callback, and its `useEffect`.
- Delete the JSX block:
  ```tsx
  {isOperator && (
    <>
      <PromptsPanel prompts={prompts} onUnauthorized={onUnauthorized} />
      <EvalHistoryPanel runs={runs} />
    </>
  )}
  ```
- Keep `isOperator`/`me`/`meError` handling only where still used (the `{!me && meError}` retry block references `meError`; keep it). If `isOperator` becomes unused after removing the block, drop it too, and simplify the trailing `{!me && meError && ...}` block if it no longer applies. Verify with tsc.

- [ ] **Step 2: Delete the unused components**

```bash
git rm dashboard/src/components/PromptsPanel.tsx dashboard/src/components/EvalHistoryPanel.tsx
```

- [ ] **Step 3: Confirm nothing else imports them**

Run: `cd dashboard && grep -rn "PromptsPanel\|EvalHistoryPanel" src`
Expected: no matches.

- [ ] **Step 4: Typecheck + build + run vitest**

Run: `cd dashboard && npx tsc --noEmit && npm run build && npx vitest run`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/DashboardPage.tsx
git commit -m "refactor(dashboard): move prompt/eval panels to Prompts tab"
```

---

## Task 22: Full verification pass

**Files:** none (verification only).

- [ ] **Step 1: Backend lint + full test suite**

Run: `ruff check gatekeep tests && ruff format --check gatekeep tests && pytest tests -q`
Expected: all pass. Fix any failures before proceeding.

- [ ] **Step 2: Frontend typecheck, tests, build**

Run: `cd dashboard && npx tsc --noEmit && npx vitest run && npm run build`
Expected: all pass.

- [ ] **Step 3: Manual E2E smoke (optional but recommended)**

Bring up the stack (per `docs`/`docker-compose.yml`), open `/dashboard`, authenticate as an operator, and confirm: the Prompts tab appears; creating a prompt, adding a version, promoting (watch the job status), setting/clearing a candidate, creating a suite + running an eval (watch the job), mining + reviewing curation, and the audit feed reflecting each action. A non-operator identity sees the "requires operator" notice.

- [ ] **Step 4: Final commit (if any fixups were needed)**

```bash
git add -A
git commit -m "chore(prompt-ops): verification fixups"
```

---

## Self-Review Notes (for the plan author; not an execution step)

Spec coverage cross-check:
- Audit table + indexes -> Task 1. `record_audit_event` in API layer -> Task 2, consumed by Tasks 6-11.
- Versions read gains template text -> Task 3. Suite/curation reads -> Task 4. Audit read -> Task 5.
- Sync mutations (create, add version, rollback, candidate set/clear, create suite, add case, mine, review) -> Tasks 6-8.
- Job backbone (Redis, Approach A, task registry, TTL) -> Task 9; poll endpoint -> Task 9.
- Async eval-run -> Task 10; async promote with eval gate -> `blocked` -> Task 11.
- Actor attribution (`actor_account_id`/`actor_label`/`created_by`) -> Tasks 6-11.
- `blocked`/`error` first-class outcomes -> Tasks 10-11 tests assert them.
- Frontend: types/client/hook -> Tasks 12-14; tab + master-detail + operator gate -> Tasks 15-16; sub-sections -> Tasks 16-19; audit feed (global + per-prompt) -> Task 20; move existing panels -> Task 21.
- Error handling: 404/400 mapping and `EvalGateFailure` -> job `blocked` -> Tasks 6-11; frontend `UnauthorizedError`/expired-job handling -> Tasks 13-20.
- Non-goals respected: no task queue (in-process asyncio + Redis), no new RBAC, account/key auditing not wired (generic table only).
