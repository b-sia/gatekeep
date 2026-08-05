# Observability Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/dashboard` the analytics surface for latency (end-to-end, provider, gateway overhead, TTFT) sliced by model/key/prompt/path, and rescope the bundled Grafana to the failure and real-time signals Postgres structurally cannot serve.

**Architecture:** A new nullable `request_logs.path` column mirrors the Prometheus `path` label exactly, written from the same parameter that already feeds `mark()`, so the two stores cannot drift. Two new read-only endpoints in `gatekeep/api/dashboard.py` compute `percentile_cont` percentiles over raw rows, reusing the existing `_base_filters` so model/key/prompt filtering comes free. Two new React panels plus one new table column render them; `grafana.json` is rebuilt around rate-limit rejections, budget alerts, and real-time tails.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (async, asyncpg), Alembic, Postgres (`percentile_cont`, `date_trunc`, aggregate `FILTER`), Pydantic v2, pytest/pytest-asyncio, React 18 + TypeScript + Vite + Recharts + Tailwind, Prometheus/Grafana.

## Global Constraints

- **Never use the em dash.** Use a plain dash `-` in all code, comments, docstrings, panel copy, README prose, and commit messages.
- **Docstrings on every function, method, and class.** Python: standard docstrings stating purpose, parameters, return values, and exceptions where applicable. TypeScript: JSDoc block comments, matching the existing `dashboard/src` style.
- **Commit messages:** conventional-commit format. Do NOT add any agent name as co-author.
- **Do not modify** `CHANGELOG.md` or any auto-generated file.
- **`request_logs.path` values are exactly** `cache_exact`, `cache_semantic`, `provider`, `stream` - the same four values `gatekeep/observability/metrics.py` documents for the Prometheus `path` label. No fifth value, no `unknown`.
- **Never backfill `path`.** Rows written before migration `0012` keep `path IS NULL` forever and are excluded from every latency query.
- **Every top-level percentile covers non-streaming paths only** (`cache_exact`, `cache_semantic`, `provider`). Streaming is reported separately as `stream_ttlt_ms` and `ttft_ms`. `duration_ms` means end-to-end on non-streaming paths and time-to-last-token on the streaming one; a blended percentile across the two is meaningless and must never be offered.
- **Percentiles are nullable, never zero.** An empty window returns `null`, and the UI renders `-`, never `0ms`.
- **Do not touch the `usage/*` endpoint response shapes.** They are stable contracts.
- **Do not change `docker-compose.yml`.** Prometheus and Grafana both stay.
- **Test database:** the suite requires `TEST_DATABASE_URL` (distinct from `DATABASE_URL`) and a running Postgres + Redis. See `.env.example`. Run tests with `pytest` from the repo root.

## Deviations from the spec (deliberate, with rationale)

Three, all flagged here so a reviewer does not read them as mistakes:

1. **The spec calls the non-streaming completion helper `_record_completion` (`gatekeep/app.py:180`).** The function is actually named `_finish_request` (`gatekeep/app.py:175`). It already takes `path` and already hands it to `mark()`, exactly as the spec describes. This plan uses the real name.
2. **`by_path` rows use the field name `key`, not `path`.** All four breakdowns share one `LatencyBreakdownRow` model with a `key` field, matching the existing `UsageBreakdownRow` convention and letting the frontend join every breakdown on the same field name. The spec wrote `{path, ...}` for this one block only.
3. **New dashboard tests are module-level functions under a section comment, not classes.** `tests/test_dashboard.py` is written entirely as flat functions with `# -- section ---` separators; adding two classes into an otherwise class-free file would be the odd style out. Coverage is identical to what the spec lists.

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `migrations/versions/0012_request_log_path.py` | Add `request_logs.path` and `ix_request_logs_created_at`. |
| `dashboard/src/components/LatencyPanel.tsx` | p50/p95 lines over time with a client-side metric toggle, plus a header stat strip. |
| `dashboard/src/components/LatencyByPathPanel.tsx` | Horizontal p50/p95 bars per path, labeled with sample counts. |

**Modified:**

| Path | Change |
|---|---|
| `gatekeep/models.py` | `RequestLog.path` column + comment. |
| `gatekeep/accounting.py` | `log_request(path=...)`; drive-by delete of the duplicated `cache_key` sentence. |
| `gatekeep/app.py` | `_finish_request` forwards `path`; `_sse` and `_messages_sse` pass `path="stream"`. |
| `gatekeep/api/dashboard.py` | Two new endpoints, shared percentile helpers, five new Pydantic models. |
| `gatekeep/observability/grafana.json` | Rescoped to ops signals. |
| `dashboard/src/api/types.ts` | Five new response interfaces. |
| `dashboard/src/api/client.ts` | Two new fetchers. |
| `dashboard/src/format.ts` | `formatMs`. |
| `dashboard/src/components/BreakdownTable.tsx` | Optional p95 column. |
| `dashboard/src/components/BreakdownPanels.tsx` | Pass latency breakdowns through. |
| `dashboard/src/pages/DashboardPage.tsx` | Two more entries in the existing `Promise.all`; render the new panels. |
| `README.md` | Restate the division of responsibility. |
| `tests/test_accounting.py`, `tests/test_endpoint.py`, `tests/test_messages_endpoint.py`, `tests/test_dashboard.py` | New coverage. |

---

### Task 1: Schema and write path for `request_logs.path`

**Files:**
- Create: `migrations/versions/0012_request_log_path.py`
- Modify: `gatekeep/models.py:72-96`, `gatekeep/accounting.py:45-116`, `gatekeep/app.py:175-256`, `gatekeep/app.py:759-838`, `gatekeep/app.py:846-915`
- Test: `tests/test_accounting.py`, `tests/test_endpoint.py`, `tests/test_messages_endpoint.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `RequestLog.path: Mapped[str | None]` - `String(32)`, nullable.
  - `log_request(..., path: str | None = None)` - new keyword-only parameter on `gatekeep.accounting.log_request`, persisted verbatim.
  - Index `ix_request_logs_created_at` on `request_logs(created_at)`.

- [ ] **Step 1: Write the failing accounting tests**

Append to `tests/test_accounting.py`:

```python
async def test_log_request_persists_path(session):
    key = ApiKey(name="path-key", key_hash="hash-path")
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-path",
        path="cache_semantic",
    )
    assert log.path == "cache_semantic"


async def test_log_request_path_defaults_to_none(session):
    """A caller with no path available must still be able to log."""
    key = ApiKey(name="no-path-key", key_hash="hash-no-path")
    session.add(key)
    await session.commit()
    await session.refresh(key)

    log = await log_request(
        session,
        key_id=key.id,
        model="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-no-path",
    )
    assert log.path is None
```

Check the imports at the top of `tests/test_accounting.py` already cover `ApiKey` and `log_request`; if `ApiKey` is missing, add it to the existing `from gatekeep.models import ...` line (import `ApiKey` alongside whatever is already there).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_accounting.py -k path -v`
Expected: FAIL with `TypeError: log_request() got an unexpected keyword argument 'path'`.

- [ ] **Step 3: Add the model column**

In `gatekeep/models.py`, inside `class RequestLog`, add `path` immediately after the `ttft_ms` column and before `__table_args__`:

```python
    # Which branch served the request: "cache_exact", "cache_semantic",
    # "provider", or "stream". Carries exactly the values the Prometheus
    # `path` label carries (observability/metrics.py), written from the same
    # parameter that feeds mark(), so the two stores cannot drift.
    #
    # NULL only on rows written before migration 0012. Nothing after the fact
    # can tell a streamed pre-0012 row from a non-streamed one, so latency
    # queries filter `path IS NOT NULL` rather than guessing.
    path: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

Then extend `__table_args__` to:

```python
    __table_args__ = (
        # Speeds up budget.get_period_spend's DB-fallback aggregate, which
        # filters by key_id and created_at >= period_start.
        Index("ix_request_logs_key_id_created_at", "key_id", "created_at"),
        # The composite above cannot serve the dashboard's time-only window
        # scans (key_id is the leading column), and percentile_cont sorts
        # every row it is handed, so narrowing the window cheaply matters.
        Index("ix_request_logs_created_at", "created_at"),
    )
```

- [ ] **Step 4: Write the migration**

Create `migrations/versions/0012_request_log_path.py`:

```python
"""add request_logs.path and a created_at index

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `path` column and the created_at-only index.

    `path` is deliberately not backfilled: any value invented for a
    pre-0012 row would be a guess, and latency queries exclude NULLs.
    """
    op.add_column("request_logs", sa.Column("path", sa.String(32), nullable=True))
    op.create_index("ix_request_logs_created_at", "request_logs", ["created_at"])


def downgrade() -> None:
    """Drop the created_at index and the `path` column."""
    op.drop_index("ix_request_logs_created_at", table_name="request_logs")
    op.drop_column("request_logs", "path")
```

- [ ] **Step 5: Thread `path` through `log_request`**

In `gatekeep/accounting.py`, add the parameter to the signature after `ttft_ms`:

```python
    ttft_ms: float | None = None,
    path: str | None = None,
) -> RequestLog:
```

Add it to the `RequestLog(...)` construction after `ttft_ms=ttft_ms,`:

```python
        ttft_ms=ttft_ms,
        path=path,
```

In the docstring, delete the duplicated sentence on line 73 (the second, stray `` `cache_key` default to a non-cache-hit request. `` - the same statement already appears at line 68 as part of `` `cached`/`cache_key` default to a non-cache-hit request. ``), and append this paragraph after the existing `duration_ms`/`provider_ms`/`ttft_ms` paragraph:

```
    `path` records which branch served the request ("cache_exact",
    "cache_semantic", "provider", or "stream"), matching the Prometheus
    `path` label one-for-one. It defaults to None so a caller without one
    can still log; pre-0012 rows are NULL and latency queries exclude them.
```

- [ ] **Step 6: Run the accounting tests to verify they pass**

Run: `pytest tests/test_accounting.py -k path -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Write the failing endpoint tests**

Append to `tests/test_endpoint.py`:

```python
async def test_non_streaming_records_path_matching_the_metric_label(
    client, raw_key, session
):
    """The DB column and the Prometheus label come from one parameter, so a
    provider-served request must land under "provider" in both."""
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.path == "provider"


async def test_cache_hit_records_cache_exact_path(client, raw_key, session):
    body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "path-cache-me"}],
    }
    headers = {"Authorization": f"Bearer {raw_key}"}
    await client.post("/v1/chat/completions", headers=headers, json=body)
    await client.post("/v1/chat/completions", headers=headers, json=body)

    logs = (
        (await session.execute(select(RequestLog).order_by(RequestLog.id)))
        .scalars()
        .all()
    )
    assert [log.path for log in logs] == ["provider", "cache_exact"]


async def test_streaming_records_stream_path(client, raw_key, session):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_lines():
            pass
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.path == "stream"
```

Append to `tests/test_messages_endpoint.py`:

```python
async def test_messages_non_streaming_records_provider_path(
    client, raw_key, session
):
    response = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.path == "provider"


async def test_messages_streaming_records_stream_path(client, raw_key, session):
    async with client.stream(
        "POST",
        "/v1/messages",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200
        async for _ in response.aiter_lines():
            pass
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.path == "stream"
```

- [ ] **Step 8: Run the endpoint tests to verify they fail**

Run: `pytest tests/test_endpoint.py tests/test_messages_endpoint.py -k path -v`
Expected: FAIL, with `assert None == 'provider'` (the column exists but nothing writes it yet).

- [ ] **Step 9: Forward `path` from `_finish_request`**

In `gatekeep/app.py`, inside `_finish_request`, add `path=path,` to the `log_request(...)` call. It goes after `routed_from=routed_from,` and before `duration_ms=timings.duration_ms,`:

```python
        routed_from=routed_from,
        path=path,
        duration_ms=timings.duration_ms,
```

In the same function's docstring, extend the `path` argument line so the invariant is stated where it is enforced:

```
        path: One of "cache_exact", "cache_semantic", "provider". Published
            as the metric label and stored on the RequestLog row from this
            one parameter, so the histogram and the column cannot diverge.
```

- [ ] **Step 10: Pass `path="stream"` from both SSE generators**

In `gatekeep/app.py`, in `_messages_sse`, add `path="stream",` to its `log_request(...)` call after `prompt_version_num=prompt_version_num,`:

```python
                        prompt_version_num=prompt_version_num,
                        path="stream",
                        duration_ms=timings.duration_ms,
```

Make the identical edit in `_sse`'s `log_request(...)` call.

- [ ] **Step 11: Run the endpoint tests to verify they pass**

Run: `pytest tests/test_endpoint.py tests/test_messages_endpoint.py -k path -v`
Expected: PASS (5 passed).

- [ ] **Step 12: Run the full Python suite**

Run: `pytest`
Expected: all tests pass, no new failures.

- [ ] **Step 13: Verify the migration applies and reverses cleanly**

Run:
```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```
Expected: three clean runs, no error. Confirm the column exists:
```bash
psql "$DATABASE_URL" -c "\d request_logs"
```
Expected: a `path | character varying(32)` row and an `ix_request_logs_created_at` index.

- [ ] **Step 14: Commit**

```bash
git add migrations/versions/0012_request_log_path.py gatekeep/models.py gatekeep/accounting.py gatekeep/app.py tests/test_accounting.py tests/test_endpoint.py tests/test_messages_endpoint.py
git commit -m "feat(observability): record the serving path on request_logs

Adds request_logs.path, carrying exactly the four values the Prometheus
path label carries, written from the same parameter that feeds mark() so
the two stores cannot drift. Not backfilled: pre-0012 rows stay NULL and
latency queries exclude them. Also adds a created_at-only index, which
the existing (key_id, created_at) composite cannot serve."
```

---

### Task 2: `GET /dashboard/api/latency/summary`

**Files:**
- Modify: `gatekeep/api/dashboard.py` (append after `usage_timeseries_by_model`, before `class EvalRunOut`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `RequestLog.path` and the `log_request(path=...)` write path from Task 1; the existing `_base_filters`, `_default_window`, `_NO_PROMPT_LABEL`, and `require_api_key`.
- Produces:
  - `class Percentiles(BaseModel)` - `p50_ms: float`, `p95_ms: float`, `p99_ms: float`.
  - `class LatencyBreakdownRow(BaseModel)` - `key: str`, `label: str | None = None`, `sample_count: int`, `p50_ms: float | None`, `p95_ms: float | None`.
  - `class LatencySummaryResponse(BaseModel)` - fields listed in Step 3.
  - `def _latency_filters(start, end, *, model, key_id, prompt_name) -> list` - `_base_filters` plus `path IS NOT NULL` and `duration_ms IS NOT NULL`.
  - `_NON_STREAMING` / `_STREAMING` - module-level SQLAlchemy boolean expressions.
  - `_OVERHEAD_MS` - module-level SQLAlchemy expression `duration_ms - COALESCE(provider_ms, 0)`.
  - `def _percentile_exprs(column, condition) -> list` - the three `percentile_cont` expressions.
  - `def _percentiles(p50, p95, p99) -> Percentiles | None`.
  - `async def _latency_breakdown(session, group_col, filters, *, condition) -> list[LatencyBreakdownRow]`.
  - `async def _latency_key_breakdown(session, filters, *, condition) -> list[LatencyBreakdownRow]`.

- [ ] **Step 1: Extend the test seeding helper**

In `tests/test_dashboard.py`, add four keyword parameters to `_seed_log` (after `created_at`) and pass them to the `RequestLog(...)` construction. Also make `response_id` unique per row, since the current f-string collides once two rows differ only in latency:

```python
async def _seed_log(
    session,
    *,
    key_id: int,
    model: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_usd: float = 0.01,
    cached: bool = False,
    prompt_name: str | None = None,
    created_at: datetime | None = None,
    path: str | None = None,
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
) -> RequestLog:
    """Insert one RequestLog row directly, optionally backdating created_at.

    `path`/`duration_ms`/`provider_ms`/`ttft_ms` default to None, matching a
    pre-0012 row: such rows are deliberately excluded from every latency
    query, so latency tests must pass `path` explicitly.
    """
    log = RequestLog(
        key_id=key_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        response_id=f"resp-{uuid4()}",
        prompt_name=prompt_name,
        path=path,
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
    )
```

Add `from uuid import uuid4` to the imports at the top of the file.

- [ ] **Step 2: Write the failing summary tests**

Append to `tests/test_dashboard.py`:

```python
# -- latency summary --------------------------------------------------------


async def _key_id(session, raw_key: str) -> int:
    """Resolve the ApiKey id backing a raw dashboard test key."""
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))
        )
    ).scalar_one()
    return row.id


async def _seed_latency_fixture(session, key_id: int) -> None:
    """Seed the shared latency fixture used by the summary/timeseries tests.

    Five latency-eligible rows plus one pre-0012 row:

      1. provider     dur=100  prov=60   gpt-4o          prompt "p1"
      2. provider     dur=200  prov=160  gpt-4o          prompt "p1"
      3. cache_exact  dur=300  prov=NULL claude-sonnet-5 no prompt (cached)
      4. stream       dur=1000 prov=900  ttft=100  gpt-4o
      5. stream       dur=2000 prov=1800 ttft=300  gpt-4o
      6. path=NULL    dur=9e9  prov=1    - must never appear anywhere
    """
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        prompt_name="p1",
        path="provider",
        duration_ms=100.0,
        provider_ms=60.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        prompt_name="p1",
        path="provider",
        duration_ms=200.0,
        provider_ms=160.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="claude-sonnet-5",
        cached=True,
        path="cache_exact",
        duration_ms=300.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=1000.0,
        provider_ms=900.0,
        ttft_ms=100.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=2000.0,
        provider_ms=1800.0,
        ttft_ms=300.0,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=9_000_000_000.0,
        provider_ms=1.0,
    )


async def test_latency_summary_requires_auth(client):
    r = await client.get("/dashboard/api/latency/summary")
    assert r.status_code == 401


async def test_latency_summary_percentiles(client, raw_key, session):
    """p50 over an odd-sized set is an actual element, so it is exact.
    p95/p99 interpolate between elements, where binary float arithmetic
    makes `==` fragile - pytest.approx's default 1e-6 relative tolerance is
    still far tighter than any real measurement difference."""
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    body = r.json()

    # Every latency-eligible row, both streaming and not; the path=NULL row
    # is excluded.
    assert body["sample_count"] == 5

    # Non-streaming durations: {100, 200, 300}.
    assert body["e2e_ms"]["p50_ms"] == 200.0
    assert body["e2e_ms"]["p95_ms"] == pytest.approx(290.0)
    assert body["e2e_ms"]["p99_ms"] == pytest.approx(298.0)

    # Non-streaming provider times: {60, 160}. The cache hit made no upstream
    # call, so its NULL drops out of the ordered set rather than counting.
    assert body["provider_ms"]["p50_ms"] == 110.0
    assert body["provider_ms"]["p95_ms"] == pytest.approx(155.0)

    # Overhead = duration - COALESCE(provider, 0): {40, 40, 300}. The cache
    # hit's entire 300ms is gatekeep's own time.
    assert body["overhead_ms"]["p50_ms"] == 40.0
    assert body["overhead_ms"]["p95_ms"] == pytest.approx(274.0)

    # Streaming time-to-last-token: {1000, 2000}.
    assert body["stream_ttlt_ms"]["p50_ms"] == 1500.0
    # Streaming TTFT: {100, 300}.
    assert body["ttft_ms"]["p50_ms"] == 200.0


async def test_latency_summary_breakdowns(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    # by_path is the one breakdown covering streaming rows too.
    by_path = {row["key"]: row for row in body["by_path"]}
    assert by_path["provider"]["sample_count"] == 2
    assert by_path["provider"]["p50_ms"] == 150.0
    assert by_path["cache_exact"]["sample_count"] == 1
    assert by_path["cache_exact"]["p50_ms"] == 300.0
    assert by_path["stream"]["sample_count"] == 2
    assert by_path["stream"]["p50_ms"] == 1500.0

    # The other three are non-streaming only, matching the top-level rule.
    by_model = {row["key"]: row for row in body["by_model"]}
    assert by_model["gpt-4o"]["sample_count"] == 2
    assert by_model["gpt-4o"]["p50_ms"] == 150.0
    assert by_model["claude-sonnet-5"]["sample_count"] == 1
    assert by_model["claude-sonnet-5"]["p50_ms"] == 300.0

    by_key = {row["key"]: row for row in body["by_key"]}
    assert by_key[str(key_id)]["sample_count"] == 3
    assert by_key[str(key_id)]["p50_ms"] == 200.0
    assert by_key[str(key_id)]["label"] == "dashboard-test"

    by_prompt = {row["key"]: row for row in body["by_prompt"]}
    assert by_prompt["p1"]["sample_count"] == 2
    assert by_prompt["p1"]["p50_ms"] == 150.0
    assert by_prompt["(none)"]["sample_count"] == 1
    assert by_prompt["(none)"]["p50_ms"] == 300.0


async def test_latency_summary_excludes_rows_with_no_path(
    client, raw_key, session
):
    """Rows written between migrations 0011 and 0012 carry timings but no
    path, and nothing can tell a streamed one from a non-streamed one."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=500.0,
        provider_ms=400.0,
    )

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()
    assert body["sample_count"] == 0
    assert body["e2e_ms"] is None
    assert body["by_path"] == []


async def test_latency_summary_empty_window_returns_nulls_not_zeros(
    client, raw_key, session
):
    """A cost-only workload must read "no data", not "0 ms"."""
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    now = datetime.now(timezone.utc)
    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(days=40)).isoformat(),
            "end": (now - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sample_count"] == 0
    assert body["e2e_ms"] is None
    assert body["provider_ms"] is None
    assert body["overhead_ms"] is None
    assert body["stream_ttlt_ms"] is None
    assert body["ttft_ms"] is None
    assert body["by_model"] == []


async def test_latency_summary_filters_by_model(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"model": "claude-sonnet-5"},
    )
    body = r.json()
    assert body["sample_count"] == 1
    assert body["e2e_ms"]["p50_ms"] == 300.0
    assert [row["key"] for row in body["by_path"]] == ["cache_exact"]
```

Add `import pytest` to the top of `tests/test_dashboard.py` if it is not already there.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k latency_summary -v`
Expected: FAIL - the auth test gets 404 instead of 401, the rest raise on `body["sample_count"]`.

- [ ] **Step 4: Add the shared latency helpers**

In `gatekeep/api/dashboard.py`, extend the SQLAlchemy import line to include `true`:

```python
from sqlalchemy import Integer, case, func, select, true
```

Then append the helpers after `usage_timeseries_by_model` and before `class EvalRunOut`:

```python
# Latency query building blocks -------------------------------------------
#
# `duration_ms` holds two different quantities depending on path: end-to-end
# on cache_exact/cache_semantic/provider, and time-to-last-token on stream
# (models.py). `provider_ms` splits the same way (one call vs. a whole
# stream), and overhead inherits the split from both. A percentile blended
# across the two would be meaningless, so every top-level figure is computed
# over one side or the other, never both.

_NON_STREAMING = RequestLog.path != "stream"
_STREAMING = RequestLog.path == "stream"

# On a cache hit no provider call was made, so the entire duration is
# gatekeep's own time. This matches the middleware's treatment of the same
# case (gatekeep/observability/latency.py).
_OVERHEAD_MS = RequestLog.duration_ms - func.coalesce(RequestLog.provider_ms, 0.0)

_QUANTILES = (0.5, 0.95, 0.99)


class Percentiles(BaseModel):
    """p50/p95/p99 of one latency quantity, in milliseconds."""

    p50_ms: float
    p95_ms: float
    p99_ms: float


class LatencyBreakdownRow(BaseModel):
    """One row of a latency breakdown, grouped by a single dimension
    (path, model id, API key id, or prompt name).

    `p50_ms`/`p95_ms` are None when the group has no qualifying rows, so a
    caller renders "-" rather than a misleading 0.
    """

    key: str
    label: str | None = None
    sample_count: int
    p50_ms: float | None
    p95_ms: float | None


def _latency_filters(
    start: datetime,
    end: datetime,
    *,
    model: str | None,
    key_id: int | None,
    prompt_name: str | None,
) -> list:
    """Build the WHERE clauses for a latency query: the usual usage filters
    plus the two latency-eligibility conditions.

    `path IS NOT NULL` excludes rows written between migrations 0011 and
    0012, which carry timings but no path - nothing after the fact can tell
    a streamed one from a non-streamed one, so they cannot be assigned to
    either side of the streaming split. This self-heals as those rows age
    out of the reporting window.
    """
    return [
        *_base_filters(
            start, end, model=model, key_id=key_id, prompt_name=prompt_name
        ),
        RequestLog.path.isnot(None),
        RequestLog.duration_ms.isnot(None),
    ]


def _percentile_exprs(column, condition) -> list:
    """Return p50/p95/p99 `percentile_cont` expressions over `column`,
    restricted to the rows matching `condition` via an aggregate FILTER.

    `percentile_cont` ignores NULLs in its sorted input, which is what makes
    `provider_ms` percentiles correct without a second filter: a cache hit's
    NULL drops out rather than counting as a zero-length provider call.
    """
    return [
        func.percentile_cont(q).within_group(column.asc()).filter(condition)
        for q in _QUANTILES
    ]


def _percentiles(p50, p95, p99) -> Percentiles | None:
    """Build a `Percentiles` from three raw aggregate values, or None when
    the ordered set was empty.

    All three come from the same input set, so a non-NULL p50 implies all
    three are non-NULL. Returning None rather than zeros keeps "no data"
    distinguishable from "0 ms" in a cost-only workload.
    """
    if p50 is None:
        return None
    return Percentiles(p50_ms=float(p50), p95_ms=float(p95), p99_ms=float(p99))


async def _latency_breakdown(
    session: AsyncSession, group_col, filters: list, *, condition
) -> list[LatencyBreakdownRow]:
    """Run one GROUP BY latency aggregate over RequestLog for `group_col`.

    `condition` restricts which rows feed the count and the percentiles
    (typically `_NON_STREAMING`, or `true()` for the by-path breakdown,
    which is the one place both sides are shown side by side). Groups with
    no qualifying rows still appear, with `sample_count` 0 and NULL
    percentiles, so a caller joining against a usage breakdown finds every
    key. Ordered by sample count descending. NULL group values render as
    `_NO_PROMPT_LABEL`, matching `_breakdown`.
    """
    sample_count = func.count(RequestLog.id).filter(condition)
    rows = (
        await session.execute(
            select(
                group_col,
                sample_count,
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
            )
            .where(*filters)
            .group_by(group_col)
            .order_by(sample_count.desc())
        )
    ).all()
    return [
        LatencyBreakdownRow(
            key=_NO_PROMPT_LABEL if value is None else str(value),
            sample_count=int(count),
            p50_ms=None if p50 is None else float(p50),
            p95_ms=None if p95 is None else float(p95),
        )
        for value, count, p50, p95 in rows
    ]


async def _latency_key_breakdown(
    session: AsyncSession, filters: list, *, condition
) -> list[LatencyBreakdownRow]:
    """Run the same aggregate as `_latency_breakdown` grouped by
    `RequestLog.key_id`, joining `ApiKey` to attach each key's display name.

    Uses an outer join so requests from a since-deleted API key still show
    up, with `label` falling back to `#<id>` - mirroring `_key_breakdown`.
    """
    sample_count = func.count(RequestLog.id).filter(condition)
    rows = (
        await session.execute(
            select(
                RequestLog.key_id,
                ApiKey.name,
                sample_count,
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(condition),
            )
            .outerjoin(ApiKey, RequestLog.key_id == ApiKey.id)
            .where(*filters)
            .group_by(RequestLog.key_id, ApiKey.name)
            .order_by(sample_count.desc())
        )
    ).all()
    return [
        LatencyBreakdownRow(
            key=str(key_id),
            label=name if name is not None else f"#{key_id}",
            sample_count=int(count),
            p50_ms=None if p50 is None else float(p50),
            p95_ms=None if p95 is None else float(p95),
        )
        for key_id, name, count, p50, p95 in rows
    ]
```

- [ ] **Step 5: Add the response model and the endpoint**

Append to `gatekeep/api/dashboard.py`, directly after `_latency_key_breakdown`:

```python
class LatencySummaryResponse(BaseModel):
    """Latency percentiles over a time range, plus breakdowns by path,
    model, API key, and prompt name.

    `sample_count` counts every latency-eligible row in the window
    regardless of path, so it reports the true window size; the narrower
    subsets each percentile block is computed over are visible per row in
    `by_path`. `e2e_ms`/`provider_ms`/`overhead_ms` cover the non-streaming
    paths only; `stream_ttlt_ms`/`ttft_ms` cover the streaming one.
    """

    start: datetime
    end: datetime
    sample_count: int
    e2e_ms: Percentiles | None
    provider_ms: Percentiles | None
    overhead_ms: Percentiles | None
    stream_ttlt_ms: Percentiles | None
    ttft_ms: Percentiles | None
    by_path: list[LatencyBreakdownRow]
    by_model: list[LatencyBreakdownRow]
    by_key: list[LatencyBreakdownRow]
    by_prompt: list[LatencyBreakdownRow]


@router.get("/latency/summary", response_model=LatencySummaryResponse)
async def latency_summary(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> LatencySummaryResponse:
    """Return latency percentiles over a time range, broken down by path,
    model, key, and prompt name.

    Same defaults and filters as `usage_summary`: `start`/`end` default to
    the trailing 7 days, and `model`/`key_id`/`prompt_name` are optional
    equality filters. Rows with no `path` (written before migration 0012)
    are excluded throughout. Every percentile block is None for an empty
    set rather than zero. Requires a valid API key (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _latency_filters(
        start, end, model=model, key_id=key_id, prompt_name=prompt_name
    )

    row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                *_percentile_exprs(RequestLog.duration_ms, _NON_STREAMING),
                *_percentile_exprs(RequestLog.provider_ms, _NON_STREAMING),
                *_percentile_exprs(_OVERHEAD_MS, _NON_STREAMING),
                *_percentile_exprs(RequestLog.duration_ms, _STREAMING),
                *_percentile_exprs(RequestLog.ttft_ms, _STREAMING),
            ).where(*filters)
        )
    ).one()
    sample_count = int(row[0])
    e2e = _percentiles(*row[1:4])
    provider = _percentiles(*row[4:7])
    overhead = _percentiles(*row[7:10])
    stream_ttlt = _percentiles(*row[10:13])
    ttft = _percentiles(*row[13:16])

    by_path = await _latency_breakdown(
        session, RequestLog.path, filters, condition=true()
    )
    by_model = await _latency_breakdown(
        session, RequestLog.model, filters, condition=_NON_STREAMING
    )
    by_key = await _latency_key_breakdown(
        session, filters, condition=_NON_STREAMING
    )
    by_prompt = await _latency_breakdown(
        session, RequestLog.prompt_name, filters, condition=_NON_STREAMING
    )

    return LatencySummaryResponse(
        start=start,
        end=end,
        sample_count=sample_count,
        e2e_ms=e2e,
        provider_ms=provider,
        overhead_ms=overhead,
        stream_ttlt_ms=stream_ttlt,
        ttft_ms=ttft,
        by_path=by_path,
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard.py -k latency_summary -v`
Expected: PASS (6 passed).

- [ ] **Step 7: Run the full Python suite**

Run: `pytest`
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add GET /dashboard/api/latency/summary

Percentiles over raw request_logs rows via percentile_cont, which is exact
where a histogram quantile interpolates within buckets, and sliceable by
key/prompt where Prometheus deliberately cannot be. Top-level figures cover
non-streaming paths only; streaming is reported separately, because
duration_ms means end-to-end on one side and time-to-last-token on the
other and a blended percentile would be meaningless."
```

---

### Task 3: `GET /dashboard/api/latency/timeseries`

**Files:**
- Modify: `gatekeep/api/dashboard.py` (append after `latency_summary`)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: everything Task 2 produced - `_latency_filters`, `_NON_STREAMING`, `_STREAMING`, `_OVERHEAD_MS`, `_default_window`.
- Produces:
  - `class LatencyTimeseriesBucket(BaseModel)` - `bucket_start: datetime`, `sample_count: int`, and eight nullable float fields: `e2e_p50_ms`, `e2e_p95_ms`, `provider_p50_ms`, `provider_p95_ms`, `overhead_p50_ms`, `overhead_p95_ms`, `ttft_p50_ms`, `ttft_p95_ms`.
  - `class LatencyTimeseriesResponse(BaseModel)` - `start`, `end`, `interval: str`, `buckets: list[LatencyTimeseriesBucket]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dashboard.py`:

```python
# -- latency timeseries -----------------------------------------------------


async def test_latency_timeseries_requires_auth(client):
    r = await client.get("/dashboard/api/latency/timeseries")
    assert r.status_code == 401


async def test_latency_timeseries_buckets_by_day(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)

    # Yesterday: two non-streaming rows only.
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=100.0,
        provider_ms=60.0,
        created_at=yesterday,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=200.0,
        provider_ms=160.0,
        created_at=yesterday,
    )
    # Today: one streaming row only.
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=1000.0,
        provider_ms=900.0,
        ttft_ms=250.0,
        created_at=now,
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={"interval": "day"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["interval"] == "day"
    assert len(body["buckets"]) == 2

    first, second = body["buckets"]
    assert first["sample_count"] == 2
    assert first["e2e_p50_ms"] == 150.0
    assert first["provider_p50_ms"] == 110.0
    # Overhead: {40, 40}.
    assert first["overhead_p50_ms"] == 40.0
    # No streamed rows yesterday.
    assert first["ttft_p50_ms"] is None

    assert second["sample_count"] == 1
    # The only row today is streamed, so every non-streaming field is null -
    # never 0, which would read as an instantaneous response.
    assert second["e2e_p50_ms"] is None
    assert second["provider_p50_ms"] is None
    assert second["overhead_p50_ms"] is None
    assert second["ttft_p50_ms"] == 250.0


async def test_latency_timeseries_excludes_rows_with_no_path(
    client, raw_key, session
):
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        duration_ms=500.0,
        provider_ms=400.0,
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert r.status_code == 200
    assert r.json()["buckets"] == []


async def test_latency_timeseries_empty_window_is_not_an_error(
    client, raw_key, session
):
    key_id = await _key_id(session, raw_key)
    await _seed_latency_fixture(session, key_id)

    now = datetime.now(timezone.utc)
    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
        params={
            "start": (now - timedelta(days=40)).isoformat(),
            "end": (now - timedelta(days=30)).isoformat(),
        },
    )
    assert r.status_code == 200
    assert r.json()["buckets"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_dashboard.py -k latency_timeseries -v`
Expected: FAIL - 404 on the auth test, `KeyError`/assertion failures on the rest.

- [ ] **Step 3: Implement the endpoint**

Append to `gatekeep/api/dashboard.py`, after `latency_summary`:

```python
class LatencyTimeseriesBucket(BaseModel):
    """One time bucket of latency percentiles, in the flat field style the
    other timeseries buckets use.

    The same streaming split as `LatencySummaryResponse` holds per bucket:
    the `e2e`/`provider`/`overhead` fields cover non-streaming paths and
    `ttft` covers the streaming one. Any field is None when that bucket had
    no qualifying rows.

    Time-to-last-token is deliberately absent: a series whose height tracks
    generation length says more about prompt mix than about gateway
    performance, so it stays a summary-level figure.
    """

    bucket_start: datetime
    sample_count: int
    e2e_p50_ms: float | None
    e2e_p95_ms: float | None
    provider_p50_ms: float | None
    provider_p95_ms: float | None
    overhead_p50_ms: float | None
    overhead_p95_ms: float | None
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None


class LatencyTimeseriesResponse(BaseModel):
    """Latency percentiles bucketed over a time range, for charting."""

    start: datetime
    end: datetime
    interval: str
    buckets: list[LatencyTimeseriesBucket]


def _optional_ms(value) -> float | None:
    """Coerce one raw percentile aggregate to a float, preserving NULL."""
    return None if value is None else float(value)


@router.get("/latency/timeseries", response_model=LatencyTimeseriesResponse)
async def latency_timeseries(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    interval: Literal["minute", "hour", "day"] = Query(default="day"),
    model: str | None = Query(default=None),
    key_id: int | None = Query(default=None),
    prompt_name: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _caller: ApiKey = Depends(require_api_key),
) -> LatencyTimeseriesResponse:
    """Return end-to-end, provider, gateway-overhead, and TTFT percentiles
    bucketed by minute, hour, or day.

    Same filters and defaults as `usage_timeseries`; `interval` selects the
    bucket width via Postgres `date_trunc`. There is deliberately no
    per-path series: `by_path` on the summary answers "how fast is a cache
    hit" without multiplying the response size. Requires a valid API key
    (`require_api_key`).
    """
    default_start, default_end = _default_window()
    start = start or default_start
    end = end or default_end
    filters = _latency_filters(
        start, end, model=model, key_id=key_id, prompt_name=prompt_name
    )

    bucket = func.date_trunc(interval, RequestLog.created_at)
    rows = (
        await session.execute(
            select(
                bucket,
                func.count(RequestLog.id),
                func.percentile_cont(0.5)
                .within_group(RequestLog.duration_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.duration_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.5)
                .within_group(RequestLog.provider_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.provider_ms.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.5)
                .within_group(_OVERHEAD_MS.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.95)
                .within_group(_OVERHEAD_MS.asc())
                .filter(_NON_STREAMING),
                func.percentile_cont(0.5)
                .within_group(RequestLog.ttft_ms.asc())
                .filter(_STREAMING),
                func.percentile_cont(0.95)
                .within_group(RequestLog.ttft_ms.asc())
                .filter(_STREAMING),
            )
            .where(*filters)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    buckets = [
        LatencyTimeseriesBucket(
            bucket_start=bucket_start,
            sample_count=int(count),
            e2e_p50_ms=_optional_ms(e2e_p50),
            e2e_p95_ms=_optional_ms(e2e_p95),
            provider_p50_ms=_optional_ms(provider_p50),
            provider_p95_ms=_optional_ms(provider_p95),
            overhead_p50_ms=_optional_ms(overhead_p50),
            overhead_p95_ms=_optional_ms(overhead_p95),
            ttft_p50_ms=_optional_ms(ttft_p50),
            ttft_p95_ms=_optional_ms(ttft_p95),
        )
        for (
            bucket_start,
            count,
            e2e_p50,
            e2e_p95,
            provider_p50,
            provider_p95,
            overhead_p50,
            overhead_p95,
            ttft_p50,
            ttft_p95,
        ) in rows
    ]
    return LatencyTimeseriesResponse(
        start=start, end=end, interval=interval, buckets=buckets
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_dashboard.py -k latency_timeseries -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full Python suite**

Run: `pytest`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add GET /dashboard/api/latency/timeseries

Same non-streaming/streaming split as the summary endpoint, held per
bucket. Time-to-last-token is deliberately not charted over time: its
height tracks generation length, which says more about prompt mix than
about gateway performance."
```

---

### Task 4: Latency panels in the SPA

**Files:**
- Create: `dashboard/src/components/LatencyPanel.tsx`, `dashboard/src/components/LatencyByPathPanel.tsx`
- Modify: `dashboard/src/api/types.ts`, `dashboard/src/api/client.ts`, `dashboard/src/format.ts`, `dashboard/src/pages/DashboardPage.tsx`

**Interfaces:**
- Consumes: `GET /dashboard/api/latency/summary` and `GET /dashboard/api/latency/timeseries` from Tasks 2 and 3, field-for-field.
- Produces:
  - `types.ts`: `Percentiles`, `LatencyBreakdownRow`, `LatencySummaryResponse`, `LatencyTimeseriesBucket`, `LatencyTimeseriesResponse`.
  - `client.ts`: `getLatencySummary(filters: UsageFilters): Promise<LatencySummaryResponse>`, `getLatencyTimeseries(filters: UsageFilters & { interval: "minute" | "hour" | "day" }): Promise<LatencyTimeseriesResponse>`.
  - `format.ts`: `formatMs(value: number | null | undefined): string`.
  - `LatencyPanel` default export, props `{ timeseries: LatencyTimeseriesResponse | null; summary: LatencySummaryResponse | null }`.
  - `LatencyByPathPanel` default export, props `{ summary: LatencySummaryResponse | null }`.

- [ ] **Step 1: Add the response types**

Append to `dashboard/src/api/types.ts`:

```ts
/** p50/p95/p99 of one latency quantity, in milliseconds. */
export interface Percentiles {
  p50_ms: number;
  p95_ms: number;
  p99_ms: number;
}

/** One row of a latency breakdown (by path, model, API key, or prompt).
 * `p50_ms`/`p95_ms` are null when the group has no qualifying samples, so
 * render "-" rather than 0ms. */
export interface LatencyBreakdownRow {
  key: string;
  label?: string | null;
  sample_count: number;
  p50_ms: number | null;
  p95_ms: number | null;
}

/** Latency percentiles for a time window, plus breakdowns.
 *
 * `e2e_ms`, `provider_ms`, and `overhead_ms` cover the non-streaming paths
 * only; `stream_ttlt_ms` and `ttft_ms` cover the streaming one. `duration_ms`
 * in the database means end-to-end on one side and time-to-last-token on the
 * other, so the two are never blended. `sample_count` counts every
 * latency-eligible row regardless of path. */
export interface LatencySummaryResponse {
  start: string;
  end: string;
  sample_count: number;
  e2e_ms: Percentiles | null;
  provider_ms: Percentiles | null;
  overhead_ms: Percentiles | null;
  stream_ttlt_ms: Percentiles | null;
  ttft_ms: Percentiles | null;
  by_path: LatencyBreakdownRow[];
  by_model: LatencyBreakdownRow[];
  by_key: LatencyBreakdownRow[];
  by_prompt: LatencyBreakdownRow[];
}

/** One bucket of latency percentiles. The `e2e`/`provider`/`overhead`
 * fields are non-streaming; `ttft` is streaming. Any field is null when
 * that bucket had no qualifying rows. */
export interface LatencyTimeseriesBucket {
  bucket_start: string;
  sample_count: number;
  e2e_p50_ms: number | null;
  e2e_p95_ms: number | null;
  provider_p50_ms: number | null;
  provider_p95_ms: number | null;
  overhead_p50_ms: number | null;
  overhead_p95_ms: number | null;
  ttft_p50_ms: number | null;
  ttft_p95_ms: number | null;
}

/** Latency percentiles bucketed over a window, for charting. */
export interface LatencyTimeseriesResponse {
  start: string;
  end: string;
  interval: "minute" | "hour" | "day";
  buckets: LatencyTimeseriesBucket[];
}
```

- [ ] **Step 2: Add the two fetchers**

In `dashboard/src/api/client.ts`, add `LatencySummaryResponse` and `LatencyTimeseriesResponse` to the existing `import type { ... } from "./types";` block (keep the list alphabetical, so they go after `EvalHistoryResponse`). Then append after `getUsageTimeseriesByModel`:

```ts
/** Fetches latency percentiles and breakdowns for the given filters. */
export function getLatencySummary(
  filters: UsageFilters,
): Promise<LatencySummaryResponse> {
  return request<LatencySummaryResponse>("latency/summary", {
    start: filters.start,
    end: filters.end,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

/** Fetches latency percentiles bucketed into minute, hourly, or daily
 * intervals, for charting over time. */
export function getLatencyTimeseries(
  filters: UsageFilters & { interval: "minute" | "hour" | "day" },
): Promise<LatencyTimeseriesResponse> {
  return request<LatencyTimeseriesResponse>("latency/timeseries", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}
```

- [ ] **Step 3: Add `formatMs`**

Append to `dashboard/src/format.ts`:

```ts
/** Formats a millisecond duration for display, switching to seconds above
 * 1000ms so multi-second streams stay readable. A null or undefined value
 * means "no samples", rendered as "-" rather than a misleading 0ms. */
export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  if (value >= 10) return `${Math.round(value)}ms`;
  return `${value.toFixed(1)}ms`;
}
```

- [ ] **Step 4: Write `LatencyPanel.tsx`**

Create `dashboard/src/components/LatencyPanel.tsx`:

```tsx
import { useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  LatencySummaryResponse,
  LatencyTimeseriesBucket,
  LatencyTimeseriesResponse,
} from "../api/types";
import { formatBucketLabel, formatMs } from "../format";

type Metric = "e2e" | "provider" | "overhead" | "ttft";

interface LatencyPanelProps {
  timeseries: LatencyTimeseriesResponse | null;
  summary: LatencySummaryResponse | null;
}

const METRIC_LABELS: Record<Metric, string> = {
  e2e: "End-to-end",
  provider: "Provider",
  overhead: "Gateway overhead",
  ttft: "TTFT",
};

// Panel copy has to state what is measured, or the gap against Grafana gets
// reported as a bug: request_logs.duration_ms stops just before the
// accounting write, so it excludes JSON serialization and the socket write
// and reads slightly lower than gatekeep_request_duration_seconds.
const METRIC_NOTES: Record<Metric, string> = {
  e2e: "Non-streaming paths only. Measured from request start to just before the accounting write, so it reads slightly below the Prometheus end-to-end span.",
  provider: "Non-streaming paths only. Cache hits made no upstream call and are excluded.",
  overhead: "Non-streaming paths only. On a cache hit the entire duration is gateway time.",
  ttft: "Streamed requests only.",
};

/** One compact label/value pair in the panel header's stat strip. */
function HeaderStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-right">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="text-sm font-medium text-slate-200">{value}</div>
    </div>
  );
}

/**
 * p50/p95 latency over time, with a metric toggle (end-to-end / provider /
 * gateway overhead / TTFT) that re-reads already-fetched data client-side
 * rather than refetching, and a header stat strip of window-wide figures.
 */
export default function LatencyPanel({ timeseries, summary }: LatencyPanelProps) {
  const [metric, setMetric] = useState<Metric>("e2e");

  const p50Key = `${metric}_p50_ms` as keyof LatencyTimeseriesBucket;
  const p95Key = `${metric}_p95_ms` as keyof LatencyTimeseriesBucket;
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: formatBucketLabel(bucket.bucket_start, timeseries.interval),
      p50: bucket[p50Key] as number | null,
      p95: bucket[p95Key] as number | null,
    })) ?? [];
  const hasSamples = data.some((row) => row.p50 !== null || row.p95 !== null);

  return (
    <div className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-slate-300">Latency</h2>
        <div className="flex items-center gap-4">
          <HeaderStat label="p50 e2e" value={formatMs(summary?.e2e_ms?.p50_ms)} />
          <HeaderStat label="p95 e2e" value={formatMs(summary?.e2e_ms?.p95_ms)} />
          <HeaderStat label="p95 TTFT" value={formatMs(summary?.ttft_ms?.p95_ms)} />
          <HeaderStat label="p50 overhead" value={formatMs(summary?.overhead_ms?.p50_ms)} />
        </div>
      </div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-2xl text-xs text-slate-500">{METRIC_NOTES[metric]}</p>
        <div className="flex gap-1">
          {(Object.keys(METRIC_LABELS) as Metric[]).map((m) => (
            <button
              key={m}
              onClick={() => setMetric(m)}
              className={`rounded px-2 py-1 text-xs ${
                metric === m ? "bg-indigo-600 text-white" : "text-slate-400 hover:bg-slate-800"
              }`}
            >
              {METRIC_LABELS[m]}
            </button>
          ))}
        </div>
      </div>
      <div className="h-72">
        {hasSamples ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={data}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
              <YAxis
                tick={{ fill: "#94a3b8", fontSize: 12 }}
                tickFormatter={(value: number) => formatMs(value)}
              />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
                labelStyle={{ color: "#e2e8f0" }}
                formatter={(value: number) => formatMs(value)}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line
                type="monotone"
                dataKey="p50"
                stroke="#6366f1"
                name="p50"
                strokeWidth={2}
                dot={false}
                connectNulls
              />
              <Line
                type="monotone"
                dataKey="p95"
                stroke="#f97316"
                name="p95"
                strokeWidth={2}
                dot={false}
                connectNulls
              />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-500">
            No {METRIC_LABELS[metric].toLowerCase()} samples for this range.
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Write `LatencyByPathPanel.tsx`**

Create `dashboard/src/components/LatencyByPathPanel.tsx`:

```tsx
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LatencySummaryResponse } from "../api/types";
import { formatMs } from "../format";

interface LatencyByPathPanelProps {
  summary: LatencySummaryResponse | null;
}

// The stream row is time-to-last-token, not end-to-end: request_logs stores
// two different quantities in duration_ms depending on path, and this is the
// one panel where both appear side by side, so the label has to say so.
const PATH_LABELS: Record<string, string> = {
  cache_exact: "Exact cache hit",
  cache_semantic: "Semantic cache hit",
  provider: "Provider call",
  stream: "Stream (to last token)",
};

/** Horizontal p50/p95 bars per serving path, each labeled with its sample
 * count. This is what shows how much wall-clock time a cache hit actually
 * saves, which neither the usage panels nor Prometheus can show. */
export default function LatencyByPathPanel({ summary }: LatencyByPathPanelProps) {
  const rows = (summary?.by_path ?? [])
    .filter((row) => row.sample_count > 0)
    .map((row) => ({
      path: `${PATH_LABELS[row.key] ?? row.key} (n=${row.sample_count})`,
      p50: row.p50_ms,
      p95: row.p95_ms,
    }));

  return (
    <div className="mx-6 mt-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-1 text-sm font-medium text-slate-300">Latency by path</h2>
      <p className="mb-3 text-xs text-slate-500">
        End-to-end per serving path. The stream row is request start to last token, a
        different quantity from the others.
      </p>
      <div className="h-72">
        {rows.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
              <XAxis
                type="number"
                tick={{ fill: "#94a3b8", fontSize: 12 }}
                tickFormatter={(value: number) => formatMs(value)}
              />
              <YAxis
                type="category"
                dataKey="path"
                width={180}
                tick={{ fill: "#94a3b8", fontSize: 12 }}
              />
              <Tooltip
                contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
                labelStyle={{ color: "#e2e8f0" }}
                formatter={(value: number) => formatMs(value)}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Bar dataKey="p50" fill="#6366f1" name="p50" />
              <Bar dataKey="p95" fill="#f97316" name="p95" />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-500">
            No latency samples for this range.
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Wire both panels into `DashboardPage`**

In `dashboard/src/pages/DashboardPage.tsx`:

Add the component imports after the `SpendSavingsPanel` import:

```tsx
import LatencyPanel from "../components/LatencyPanel";
import LatencyByPathPanel from "../components/LatencyByPathPanel";
```

Add `getLatencySummary` and `getLatencyTimeseries` to the `from "../api/client"` import block, and `LatencySummaryResponse` / `LatencyTimeseriesResponse` to the `from "../api/types"` type import block.

Add two state hooks after `const [byModel, setByModel] = ...`:

```tsx
  const [latency, setLatency] = useState<LatencySummaryResponse | null>(null);
  const [latencySeries, setLatencySeries] = useState<LatencyTimeseriesResponse | null>(null);
```

Extend the existing `Promise.all` destructuring and array (the two new calls inherit the `UnauthorizedError` path and the error banner with no new error handling):

```tsx
      const [summaryRes, timeseriesRes, byModelRes, latencyRes, latencySeriesRes, evalsRes, promptsRes] =
        await Promise.all([
          getUsageSummary({ ...windowParams, model: filters.model ?? undefined }),
          getUsageTimeseries({
            ...windowParams,
            interval: filters.interval,
            model: filters.model ?? undefined,
          }),
          getUsageTimeseriesByModel({
            ...windowParams,
            interval: filters.interval,
            model: filters.model ?? undefined,
          }),
          getLatencySummary({ ...windowParams, model: filters.model ?? undefined }),
          getLatencyTimeseries({
            ...windowParams,
            interval: filters.interval,
            model: filters.model ?? undefined,
          }),
          getEvalHistory(),
          getPrompts(),
        ]);
      setSummary(summaryRes);
      setTimeseries(timeseriesRes);
      setByModel(byModelRes);
      setLatency(latencyRes);
      setLatencySeries(latencySeriesRes);
      setRuns(evalsRes.runs);
      setPrompts(promptsRes.prompts);
```

Render the two panels between `SpendSavingsPanel` and `BreakdownPanels` - cost story first, then speed, then attribution:

```tsx
      <SpendSavingsPanel timeseries={timeseries} />
      <LatencyPanel timeseries={latencySeries} summary={latency} />
      <LatencyByPathPanel summary={latency} />
      <BreakdownPanels summary={summary} />
```

- [ ] **Step 7: Type-check and build**

Run: `cd dashboard && npm install && npm run build`
Expected: `tsc` reports no errors and Vite writes the bundle without warnings about missing exports.

- [ ] **Step 8: Verify against live data**

Start the stack (`docker compose up -d`, then the gateway), send a mix of traffic - a provider-served request, a repeat of it to hit the exact cache, and a streamed request - then open `http://localhost:8100/dashboard` and confirm:
- The Latency panel plots p50/p95 and the toggle switches metric with no network request (check the browser Network tab stays quiet on toggle).
- "Latency by path" shows the cache-hit bar visibly shorter than the provider bar, each with its `n=` count, and the stream row labeled as time-to-last-token.
- A window with no traffic renders the explicit empty states, not `0ms`.

- [ ] **Step 9: Commit**

```bash
git add dashboard/src/api/types.ts dashboard/src/api/client.ts dashboard/src/format.ts dashboard/src/components/LatencyPanel.tsx dashboard/src/components/LatencyByPathPanel.tsx dashboard/src/pages/DashboardPage.tsx
git commit -m "feat(dashboard): surface latency in the SPA

Adds a latency panel (p50/p95 over time with a client-side metric toggle)
and a by-path panel, which is what finally shows the wall-clock saving of
a cache hit. Panel copy states what duration_ms actually measures, since
it stops before response serialization and so reads below the Prometheus
end-to-end span for identical traffic."
```

---

### Task 5: p95 column on the breakdown tables

**Files:**
- Modify: `dashboard/src/components/BreakdownTable.tsx`, `dashboard/src/components/BreakdownPanels.tsx`, `dashboard/src/pages/DashboardPage.tsx`

**Interfaces:**
- Consumes: `LatencySummaryResponse.by_model` / `.by_key` / `.by_prompt` from Task 4's types, and the `latency` state from Task 4's `DashboardPage`.
- Produces: `BreakdownTable` gains an optional `latencyRows?: LatencyBreakdownRow[]` prop; `BreakdownPanels` gains a required `latency: LatencySummaryResponse | null` prop.

- [ ] **Step 1: Add the column to `BreakdownTable`**

Replace the contents of `dashboard/src/components/BreakdownTable.tsx` with:

```tsx
import type { LatencyBreakdownRow, UsageBreakdownRow } from "../api/types";
import { formatMs } from "../format";

interface BreakdownTableProps {
  title: string;
  rows: UsageBreakdownRow[];
  /** Latency rows for the same dimension, joined on `key`. Rows with no
   * latency samples render "-", never 0ms. Omit to hide the p95 column. */
  latencyRows?: LatencyBreakdownRow[];
}

/** A titled table of usage breakdown rows, with a relative cost bar per row
 * scaled against the row with the highest cost, and an optional p95 latency
 * column joined in on `key`. */
export default function BreakdownTable({ title, rows, latencyRows }: BreakdownTableProps) {
  const maxCost = Math.max(1e-9, ...rows.map((row) => row.cost_usd));
  const p95ByKey = new Map(
    (latencyRows ?? []).map((row) => [row.key, row.sample_count > 0 ? row.p95_ms : null]),
  );
  const showLatency = latencyRows !== undefined;
  const columnCount = showLatency ? 5 : 4;

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">{title}</h2>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Name</th>
            <th className="pb-2 text-right">Requests</th>
            <th className="pb-2 text-right">Tokens</th>
            <th className="pb-2 text-right">Cost</th>
            {showLatency && (
              <th className="pb-2 text-right" title="Non-streaming requests only">
                p95
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-slate-800">
              <td className="py-2 pr-2">
                <div className="text-slate-200">{row.label ?? row.key}</div>
                <div className="h-1 w-full rounded bg-slate-800">
                  <div
                    className="h-1 rounded bg-indigo-500"
                    style={{ width: `${(row.cost_usd / maxCost) * 100}%` }}
                  />
                </div>
              </td>
              <td className="py-2 text-right text-slate-300">{row.request_count}</td>
              <td className="py-2 text-right text-slate-300">{row.total_tokens.toLocaleString()}</td>
              <td className="py-2 text-right text-slate-300">${row.cost_usd.toFixed(2)}</td>
              {showLatency && (
                <td className="py-2 text-right text-slate-300">
                  {formatMs(p95ByKey.get(row.key) ?? null)}
                </td>
              )}
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={columnCount} className="py-4 text-center text-slate-500">
                No data for this range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Pass the latency breakdowns through `BreakdownPanels`**

Replace the contents of `dashboard/src/components/BreakdownPanels.tsx` with:

```tsx
import BreakdownTable from "./BreakdownTable";
import type { LatencySummaryResponse, UsageSummaryResponse } from "../api/types";

interface BreakdownPanelsProps {
  summary: UsageSummaryResponse | null;
  latency: LatencySummaryResponse | null;
}

/** Renders the three usage breakdown tables (by model, by API key, by
 * prompt) side by side, each carrying a p95 latency column joined from the
 * latency summary. The p95 figures cover non-streaming requests only,
 * matching the rest of the latency surface. */
export default function BreakdownPanels({ summary, latency }: BreakdownPanelsProps) {
  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 lg:grid-cols-3">
      <BreakdownTable
        title="Cost by model"
        rows={summary?.by_model ?? []}
        latencyRows={latency?.by_model ?? []}
      />
      <BreakdownTable
        title="Cost by API key"
        rows={summary?.by_key ?? []}
        latencyRows={latency?.by_key ?? []}
      />
      <BreakdownTable
        title="Cost by prompt"
        rows={summary?.by_prompt ?? []}
        latencyRows={latency?.by_prompt ?? []}
      />
    </div>
  );
}
```

- [ ] **Step 3: Pass `latency` in from `DashboardPage`**

In `dashboard/src/pages/DashboardPage.tsx`, change:

```tsx
      <BreakdownPanels summary={summary} />
```

to:

```tsx
      <BreakdownPanels summary={summary} latency={latency} />
```

- [ ] **Step 4: Type-check and build**

Run: `cd dashboard && npm run build`
Expected: `tsc` reports no errors.

- [ ] **Step 5: Verify against live data**

Reload `http://localhost:8100/dashboard` and confirm the three breakdown tables show a p95 column; a key or prompt that has only ever streamed shows `-`, not `0ms`; and hovering the p95 header shows the "Non-streaming requests only" tooltip.

- [ ] **Step 6: Commit**

```bash
git add dashboard/src/components/BreakdownTable.tsx dashboard/src/components/BreakdownPanels.tsx dashboard/src/pages/DashboardPage.tsx
git commit -m "feat(dashboard): add a p95 column to the breakdown tables

Per-key and per-prompt latency attribution is the capability that justifies
reading latency out of Postgres rather than Prometheus, and it lands here at
the cost of one column rather than a new panel. Rows with no samples render
a dash, never 0ms."
```

---

### Task 6: Rescope the bundled Grafana to an ops view

**Files:**
- Modify: `gatekeep/observability/grafana.json`

**Interfaces:**
- Consumes: nothing from earlier tasks. Reads Prometheus metrics that already exist: `gatekeep_rate_limit_rejections_total`, `gatekeep_budget_alerts_total`, `gatekeep_request_duration_seconds`, `gatekeep_gateway_overhead_seconds`, `gatekeep_ttft_seconds`.
- Produces: nothing consumed by later tasks. Task 7 references the retitled dashboard in prose.

Note: the `uid` changes from `gatekeep-overview` to `gatekeep-ops` to match the retitle. Nothing else in the repo references the old uid (verified: the only occurrence is inside this file), and the dashboard is file-provisioned rather than bookmarked from a database.

- [ ] **Step 1: Replace the dashboard definition**

Replace the entire contents of `gatekeep/observability/grafana.json` with:

```json
{
  "title": "Gatekeep - Ops",
  "uid": "gatekeep-ops",
  "schemaVersion": 39,
  "version": 1,
  "editable": true,
  "timezone": "browser",
  "time": {
    "from": "now-6h",
    "to": "now"
  },
  "refresh": "30s",
  "panels": [
    {
      "id": 1,
      "title": "Rate limit rejections",
      "description": "429s never reach request_logs, so this signal exists only here.",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "fieldConfig": {
        "defaults": { "unit": "short" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum(increase(gatekeep_rate_limit_rejections_total[5m]))",
          "legendFormat": "429s (rate limited)"
        }
      ]
    },
    {
      "id": 2,
      "title": "Budget alerts by threshold",
      "description": "A budget rejection writes no request_logs row either. Alert on 'exceeded'.",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "fieldConfig": {
        "defaults": { "unit": "short" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum(increase(gatekeep_budget_alerts_total[5m])) by (threshold)",
          "legendFormat": "{{threshold}}"
        }
      ]
    },
    {
      "id": 3,
      "title": "Request rate by model and path",
      "description": "Live throughput. Cost, savings, cache hit rate, and token averages live on /dashboard, computed exactly from request_logs rather than extrapolated by increase().",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "fieldConfig": {
        "defaults": { "unit": "reqps" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum(rate(gatekeep_request_duration_seconds_count[5m])) by (model, path)",
          "legendFormat": "{{model}} / {{path}}"
        }
      ]
    },
    {
      "id": 4,
      "title": "End-to-end p95 by path",
      "description": "The full ASGI span, so slightly higher than /dashboard's figure, which stops before response serialization.",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "fieldConfig": {
        "defaults": { "unit": "s" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "histogram_quantile(0.95, sum(rate(gatekeep_request_duration_seconds_bucket[5m])) by (le, path))",
          "legendFormat": "{{path}}"
        }
      ]
    },
    {
      "id": 5,
      "title": "Gateway overhead p95 by path",
      "description": "Request time not spent upstream, from the same span as end-to-end. On a cache hit this is the entire duration.",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 16 },
      "fieldConfig": {
        "defaults": { "unit": "s" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "histogram_quantile(0.95, sum(rate(gatekeep_gateway_overhead_seconds_bucket[5m])) by (le, path))",
          "legendFormat": "{{path}}"
        }
      ]
    },
    {
      "id": 6,
      "title": "Time to first token p95",
      "description": "Streamed requests only. Per-key and per-prompt TTFT attribution lives on /dashboard: key_id is deliberately not a metric label.",
      "type": "timeseries",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 16 },
      "fieldConfig": {
        "defaults": { "unit": "s" },
        "overrides": []
      },
      "targets": [
        {
          "refId": "A",
          "expr": "histogram_quantile(0.95, sum(rate(gatekeep_ttft_seconds_bucket[5m])) by (le, model))",
          "legendFormat": "{{model}}"
        }
      ]
    }
  ]
}
```

- [ ] **Step 2: Verify the JSON parses**

Run: `python -c "import json; d = json.load(open('gatekeep/observability/grafana.json')); print(d['title'], len(d['panels']))"`
Expected: `Gatekeep - Ops 6`

- [ ] **Step 3: Verify every referenced metric actually exists**

Run:
```bash
grep -oE 'gatekeep_[a-z_]+' gatekeep/observability/grafana.json | sed -E 's/_(bucket|count|sum|total)$//' | sort -u
```
Cross-check each name against `gatekeep/observability/metrics.py`. Expected set: `gatekeep_budget_alerts`, `gatekeep_gateway_overhead_seconds`, `gatekeep_rate_limit_rejections`, `gatekeep_request_duration_seconds`, `gatekeep_ttft_seconds`. All five are declared in `metrics.py`.

- [ ] **Step 4: Verify the dashboard loads**

Run: `docker compose up -d grafana prometheus`, then open `http://localhost:3000`. Confirm the dashboard is titled "Gatekeep - Ops", all six panels render, and no panel shows a datasource or query error. Send some traffic through the gateway and confirm the request-rate panel moves.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/observability/grafana.json
git commit -m "refactor(observability): rescope the bundled Grafana to an ops view

Drops cost per model, cumulative savings, cache hit rate, and avg tokens
per request: the SPA owns those and computes them exactly where increase()
extrapolates. What is left is either a failure signal that never reaches
request_logs (429s, budget alerts) or a real-time tail worth alerting on."
```

---

### Task 7: State the division of responsibility in the README

**Files:**
- Modify: `README.md:63-71`, `README.md:151`, `README.md:179-195`

**Interfaces:**
- Consumes: everything above - the new endpoints, the new panels, and the retitled Grafana dashboard.
- Produces: nothing.

- [ ] **Step 1: Align the project-layout rows**

In the project layout table (around lines 63-71), replace the `gatekeep/observability/` row with:

```markdown
| `gatekeep/observability/` | Prometheus metric definitions, plus the ops Grafana dashboard `docker-compose.yml` provisions |
```

and the `dashboard/` row with:

```markdown
| `dashboard/` | First-party React/TypeScript dashboard SPA, served by the gateway at `/dashboard` - the analytics surface for cost, usage, and latency; see "Dashboard" below |
```

- [ ] **Step 2: Reframe `/metrics` as the integration surface**

Replace the paragraph at line 151:

```markdown
`/metrics` is a Prometheus-format endpoint (unauthenticated, like `/healthz`); `docker-compose.yml` also runs Prometheus and a Grafana dashboard at `http://localhost:3000` for cost, usage, and cache-hit-rate visualization.
```

with:

```markdown
`/metrics` is a Prometheus-format endpoint (unauthenticated, like `/healthz`)
and is the **integration surface**: scrape it into whatever Prometheus you
already run. `docker-compose.yml` also brings up Prometheus and a small
"Gatekeep - Ops" Grafana dashboard at `http://localhost:3000` as a
local-development convenience, scoped to the signals Postgres structurally
cannot serve - rate-limit rejections, budget alerts, and real-time latency
tails. Cost, savings, cache-hit rate, and latency attribution live on
`/dashboard`, which computes them exactly from `request_logs` rather than
extrapolating from histogram buckets.
```

- [ ] **Step 3: Document `request_logs.path`**

Replace the paragraph at lines 179-183:

```markdown
Per-request latency is also stored on `request_logs` as `duration_ms`,
`provider_ms`, and `ttft_ms`. `provider_ms` is NULL on a cache hit and
`ttft_ms` is NULL on any non-streamed request. A NULL `provider_ms` alone
cannot distinguish a cache hit from a row predating the migration - filter on
`cached`.
```

with:

```markdown
Per-request latency is also stored on `request_logs` as `duration_ms`,
`provider_ms`, `ttft_ms`, and `path`. `provider_ms` is NULL on a cache hit and
`ttft_ms` is NULL on any non-streamed request. A NULL `provider_ms` alone
cannot distinguish a cache hit from a row predating the migration - filter on
`cached`. `path` carries the same four values as the Prometheus `path` label
(`cache_exact`, `cache_semantic`, `provider`, `stream`) and is written from
the same parameter, so the two stores cannot drift; it is NULL only on rows
predating migration `0012`, which every latency query excludes.

`duration_ms` means two different things depending on `path`: end-to-end on
the non-streaming paths, and time-to-last-token on `stream`. Percentiles are
never blended across the two.
```

- [ ] **Step 4: Update the Dashboard section**

Replace the paragraph at lines 189-195:

```markdown
Once the gateway is running, the first-party dashboard is served at
`http://localhost:8100/dashboard` - a cost/usage/eval-history view over the
same data as the Grafana dashboard above, plus prompt version history. On
first load it prompts for an API key (the same kind used for
`/v1/chat/completions`); the key is stored in the browser's `localStorage`
and sent as a bearer token to the dashboard's own read-only API under
`/dashboard/api/*`.
```

with:

```markdown
Once the gateway is running, the first-party dashboard is served at
`http://localhost:8100/dashboard`. It is the **analytics surface**: cost,
usage, cache savings, latency (end-to-end, provider, gateway overhead, and
TTFT, broken down by path, model, key, and prompt), prompt version history,
and eval history - all read from `request_logs` and the prompt/eval tables,
filterable by model and time window. Per-key and per-prompt latency
attribution lives here rather than in Prometheus because `key_id` is
deliberately not a metric label: the wide latency bucket set would put the
per-key series count around 108,000 against 1,100 without it.

On first load it prompts for an API key (the same kind used for
`/v1/chat/completions`); the key is stored in the browser's `localStorage`
and sent as a bearer token to the dashboard's own read-only API under
`/dashboard/api/*`.

One caveat worth knowing before comparing the two surfaces: `/dashboard`
reads slightly lower than Grafana for identical traffic. `request_logs.duration_ms`
stops just before the accounting write, so it excludes JSON serialization and
the socket write, where `gatekeep_request_duration_seconds` covers the full
ASGI span.
```

- [ ] **Step 5: Verify no stale claims remain**

Run: `grep -n "same data as the Grafana\|cache-hit-rate visualization\|Prometheus/Grafana config" README.md`
Expected: no output. Also run `grep -n "—" README.md` and expect no output (no em dashes).

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: state the /metrics vs /dashboard division of responsibility

/metrics is the integration surface, /dashboard is the analytics surface,
and the bundled Grafana is a local ops view. Also documents request_logs.path
and the fact that duration_ms holds two different quantities depending on it."
```

---

## Final verification

- [ ] Run the full Python suite: `pytest`. Expected: all pass.
- [ ] Run the frontend gate: `cd dashboard && npm run build`. Expected: no `tsc` errors.
- [ ] Confirm migrations are linear: `alembic history` shows `0011 -> 0012` with no branch.
- [ ] Confirm no em dashes were introduced: `git diff master --unified=0 | grep -n "—"` returns nothing.
- [ ] Manual acceptance against live seeded traffic: `/dashboard` shows latency over time, latency by path with a visibly cheaper cache-hit bar, and p95 columns on all three breakdown tables; `http://localhost:3000` shows "Gatekeep - Ops" with six working panels and none of the cost/savings/cache-rate panels it used to carry.
