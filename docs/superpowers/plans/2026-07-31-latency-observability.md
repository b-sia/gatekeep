# Latency Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record end-to-end latency, upstream provider latency, time-to-first-token, and inter-token latency for every request, in both Prometheus and Postgres.

**Architecture:** A pure ASGI middleware stamps a start time onto `scope["state"]` before any FastAPI dependency runs, and observes the end-to-end histogram on the way out for non-streaming responses. The endpoints publish `model` and `path` back onto `request.state` as they resolve them, since a middleware cannot know either. Streaming self-reports from inside the SSE generators via a `StreamTimer`, because `StreamingResponse` returns before the provider is ever called. All timing primitives live in one new module, `gatekeep/observability/latency.py`, so `app.py` gains call sites but no timing logic.

**Tech Stack:** Python 3, FastAPI/Starlette 0.52, SQLAlchemy async, Alembic, `prometheus_client`, pytest + pytest-asyncio.

## Global Constraints

- Spec of record: `docs/superpowers/specs/2026-07-31-latency-observability-design.md`. Read it before starting.
- **Never use the em dash character.** Use a plain `-`.
- Every function, method, and class gets a docstring stating purpose, parameters, return values, and exceptions where applicable.
- `ruff` is the linter and formatter. Run `ruff check .` and `ruff format .` before every commit.
- **Instrumentation must never fail a request.** Missing timing state produces `None` columns and skipped observations, never an exception.
- Do not change the labels of any existing metric. `request_tokens` and `request_cost_usd` keep `[model, key_id]`.
- Latency histograms are labeled `[model, path]` or `[model]`, never `key_id`.
- Tests need live Postgres and Redis: `docker-compose up -d postgres redis`. `TEST_DATABASE_URL` must differ from `DATABASE_URL`; the suite drops and recreates the schema on every test.
- Commit after every task.

---

### Task 1: Latency histograms

**Files:**
- Modify: `gatekeep/observability/metrics.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `LATENCY_BUCKETS_WIDE`, `LATENCY_BUCKETS_TIGHT`, and the histograms `request_duration_seconds`, `provider_duration_seconds`, `gateway_overhead_seconds`, `ttft_seconds`, `inter_token_seconds`. All are `prometheus_client.Histogram`. `request_duration_seconds` and `gateway_overhead_seconds` take labels `(model, path)`; the other three take `(model,)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_metrics.py`:

```python
# -- latency histograms ------------------------------------------------


def test_latency_histograms_have_llm_appropriate_buckets():
    """Default prometheus buckets stop at 10s, which is useless for LLM traffic."""
    from gatekeep.observability import metrics

    assert max(metrics.LATENCY_BUCKETS_WIDE) == 120
    assert min(metrics.LATENCY_BUCKETS_WIDE) == 0.005
    assert max(metrics.LATENCY_BUCKETS_TIGHT) == 2


def test_request_duration_seconds_is_labeled_by_model_and_path():
    from gatekeep.observability import metrics

    metrics.request_duration_seconds.labels(
        model="test-latency-model", path="provider"
    ).observe(1.5)
    samples = metrics.request_duration_seconds.collect()[0].samples
    assert any(
        s.name.endswith("_sum")
        and s.labels == {"model": "test-latency-model", "path": "provider"}
        and s.value == pytest.approx(1.5)
        for s in samples
    )


def test_latency_histograms_are_not_labeled_by_key_id():
    """key_id is unbounded; per-key latency comes from Postgres instead."""
    from gatekeep.observability import metrics

    for histogram in (
        metrics.request_duration_seconds,
        metrics.provider_duration_seconds,
        metrics.gateway_overhead_seconds,
        metrics.ttft_seconds,
        metrics.inter_token_seconds,
    ):
        assert "key_id" not in histogram._labelnames


def test_single_label_histograms_take_model_only():
    from gatekeep.observability import metrics

    metrics.ttft_seconds.labels(model="test-ttft-model").observe(0.25)
    metrics.inter_token_seconds.labels(model="test-ttft-model").observe(0.01)
    metrics.provider_duration_seconds.labels(model="test-ttft-model").observe(2.0)
    samples = metrics.ttft_seconds.collect()[0].samples
    assert any(
        s.name.endswith("_count") and s.labels == {"model": "test-ttft-model"}
        for s in samples
    )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_metrics.py -k latency -v`
Expected: FAIL with `AttributeError: module 'gatekeep.observability.metrics' has no attribute 'LATENCY_BUCKETS_WIDE'`

- [ ] **Step 3: Add the histograms**

Append to `gatekeep/observability/metrics.py`:

```python
# Latency buckets. prometheus_client's defaults top out at 10s, which cannot
# describe LLM traffic. The wide set's low end matters because cache hits
# return in single-digit milliseconds and would otherwise all land in one
# bucket with provider calls.
LATENCY_BUCKETS_WIDE = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120,
)
LATENCY_BUCKETS_TIGHT = (0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2)

# `path` is one of "cache_exact", "cache_semantic", "provider", "stream". One
# label replaces separate `cached`/`streaming` labels: streaming returns before
# any cache lookup runs (app.py), so the two can never co-occur.
#
# WARNING: this metric carries two different definitions of end-to-end,
# separated only by `path`. For path="stream" it is start until the last token,
# recorded by the SSE generator. For every other path it is the full ASGI span,
# recorded by LatencyMiddleware. Aggregating across all paths mixes the two;
# queries and alerts must pin `path` or at minimum exclude "stream".
request_duration_seconds = Histogram(
    "gatekeep_request_duration_seconds",
    "End-to-end request latency in seconds.",
    ["model", "path"],
    buckets=LATENCY_BUCKETS_WIDE,
)

provider_duration_seconds = Histogram(
    "gatekeep_provider_duration_seconds",
    "Time spent in the upstream provider call, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_WIDE,
)

# Not derived in PromQL: subtracting two histograms is not a statistically
# valid operation. On a cache hit there is no provider call, so the whole
# duration is gateway time and is recorded here in full.
gateway_overhead_seconds = Histogram(
    "gatekeep_gateway_overhead_seconds",
    "Request time not spent in the upstream provider, in seconds.",
    ["model", "path"],
    buckets=LATENCY_BUCKETS_WIDE,
)

ttft_seconds = Histogram(
    "gatekeep_ttft_seconds",
    "Time to first token on a streamed completion, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_TIGHT,
)

# Providers do not guarantee one token per delta (Anthropic's text_stream
# yields text pieces; Ollama yields per-token), so this is really inter-chunk
# latency in provider-native units. The token-normalized figure is the
# DB-derived mean: (duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0).
inter_token_seconds = Histogram(
    "gatekeep_inter_token_seconds",
    "Gap between consecutive streamed text deltas, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_TIGHT,
)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_metrics.py -k latency -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && ruff format .
git add gatekeep/observability/metrics.py tests/test_metrics.py
git commit -m "feat(observability): add latency histograms with LLM-appropriate buckets"
```

---

### Task 2: Schema columns, migration, and `log_request` kwargs

**Files:**
- Modify: `gatekeep/models.py`
- Create: `migrations/versions/0011_request_latency.py`
- Modify: `gatekeep/accounting.py`
- Test: `tests/test_accounting.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `RequestLog.duration_ms`, `RequestLog.provider_ms`, `RequestLog.ttft_ms`, all `Mapped[float | None]`. `log_request` gains keyword-only `duration_ms: float | None = None`, `provider_ms: float | None = None`, `ttft_ms: float | None = None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_accounting.py`:

```python
async def test_log_request_records_latency_columns(session):
    """Timing kwargs land on the row; omitting them leaves NULLs."""
    key = ApiKey(name="latency", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()

    timed = await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-timed",
        duration_ms=1234.5,
        provider_ms=1200.0,
        ttft_ms=300.0,
    )
    assert timed.duration_ms == pytest.approx(1234.5)
    assert timed.provider_ms == pytest.approx(1200.0)
    assert timed.ttft_ms == pytest.approx(300.0)


async def test_log_request_latency_columns_default_to_none(session):
    """A caller with no timing available must still be able to log."""
    key = ApiKey(name="untimed", key_hash=hash_key(generate_key()))
    session.add(key)
    await session.commit()

    untimed = await log_request(
        session,
        key_id=key.id,
        model="claude-sonnet-5",
        prompt_tokens=10,
        completion_tokens=5,
        response_id="resp-untimed",
    )
    assert untimed.duration_ms is None
    assert untimed.provider_ms is None
    assert untimed.ttft_ms is None
```

If `pytest`, `ApiKey`, `generate_key`, or `hash_key` are not already imported in that file, add:

```python
import pytest

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_accounting.py -k latency -v`
Expected: FAIL with `TypeError: log_request() got an unexpected keyword argument 'duration_ms'`

- [ ] **Step 3: Add the columns**

In `gatekeep/models.py`, inside `class RequestLog`, after the `prompt_version_num` column and before `__table_args__`:

```python
    # Latency, in milliseconds. All three are nullable because each is
    # genuinely undefined in some cases, not merely unknown:
    #   duration_ms: request start until just before log_request. On the
    #     non-streaming path this excludes JSON serialization and the socket
    #     write, so it is very slightly smaller than the full-ASGI figure in
    #     gatekeep_request_duration_seconds. On the streaming path
    #     log_request fires at StreamEnd, so it genuinely is time-to-last-token.
    #   provider_ms: time in the upstream call. NULL on a cache hit (no call
    #     was made). A NULL alone cannot distinguish a cache hit from a row
    #     predating this migration - disambiguate on `cached`, never on
    #     `provider_ms IS NULL`.
    #   ttft_ms: time to first token. NULL on every non-streamed request,
    #     where the concept does not exist.
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ttft_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
```

- [ ] **Step 4: Write the migration**

Create `migrations/versions/0011_request_latency.py`:

```python
"""add latency columns (duration_ms, provider_ms, ttft_ms) to request_logs

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_logs", sa.Column("duration_ms", sa.Float(), nullable=True))
    op.add_column("request_logs", sa.Column("provider_ms", sa.Float(), nullable=True))
    op.add_column("request_logs", sa.Column("ttft_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("request_logs", "ttft_ms")
    op.drop_column("request_logs", "provider_ms")
    op.drop_column("request_logs", "duration_ms")
```

- [ ] **Step 5: Add the `log_request` kwargs**

In `gatekeep/accounting.py`, add to the signature after `prompt_version_num`:

```python
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
```

Add to the `RequestLog(...)` construction:

```python
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
```

Append to the docstring, before the closing quotes:

```
    `duration_ms`/`provider_ms`/`ttft_ms` are optional timing in milliseconds,
    defaulting to None so any caller without timing available can still log.
    `provider_ms` is None on a cache hit (no upstream call was made) and
    `ttft_ms` is None on any non-streamed request (the concept does not
    exist there). See gatekeep/observability/latency.py.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pytest tests/test_accounting.py -k latency -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Verify the migration applies cleanly**

Run: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head`
Expected: no errors. This confirms `downgrade` is correct, not just `upgrade`.

- [ ] **Step 8: Lint and commit**

```bash
ruff check . && ruff format .
git add gatekeep/models.py gatekeep/accounting.py migrations/versions/0011_request_latency.py tests/test_accounting.py
git commit -m "feat(models): add latency columns to request_logs"
```

---

### Task 3: Timing primitives and middleware

**Files:**
- Create: `gatekeep/observability/latency.py`
- Modify: `gatekeep/app.py` (register the middleware only)
- Test: `tests/test_latency.py` (new)

**Interfaces:**
- Consumes: the five histograms from Task 1.
- Produces:
  - `@dataclass LatencyTimings` with fields `duration_ms: float | None`, `provider_ms: float | None`, `ttft_ms: float | None`.
  - `class LatencyMiddleware` - pure ASGI, constructed as `LatencyMiddleware(app)`.
  - `def mark(request, *, model=None, path=None) -> None` - publishes labels onto `request.state`.
  - `def observe_non_streaming(request, *, model, path, provider_ms=None) -> LatencyTimings`.
  - `class StreamTimer` with `__init__(started_at: float | None, *, model: str)`, `provider_started() -> None`, `delta() -> None`, `finish() -> LatencyTimings`.

**Why a pure ASGI middleware and not `BaseHTTPMiddleware`:** `BaseHTTPMiddleware` wraps the response body in an anyio stream and has known awkward interactions with long-lived streaming responses. A pure ASGI middleware avoids that entirely. `request.state` is backed by `scope["state"]` in Starlette 0.52 (`HTTPConnection.state` does `self.scope.setdefault("state", {})`), so the middleware and the endpoints share one dict.

- [ ] **Step 1: Write the failing test**

Create `tests/test_latency.py`:

```python
"""Unit tests for the latency timing primitives, with no live app involved."""

import pytest

from gatekeep.observability import metrics
from gatekeep.observability.latency import (
    LatencyTimings,
    StreamTimer,
    mark,
    observe_non_streaming,
)


class _FakeState:
    """Stand-in for starlette's State, which is attribute-access over a dict."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeRequest:
    """Minimal stand-in exposing only the `.state` the helpers touch."""

    def __init__(self, **state):
        self.state = _FakeState(**state)


def _sum_for(histogram, labels):
    """Return a histogram's _sum sample value for an exact label set, or None."""
    for sample in histogram.collect()[0].samples:
        if sample.name.endswith("_sum") and sample.labels == labels:
            return sample.value
    return None


def test_observe_non_streaming_returns_duration_and_provider_ms():
    request = _FakeRequest(started_at=0.0)
    timings = observe_non_streaming(
        request, model="m-obs-1", path="provider", provider_ms=50.0
    )
    assert timings.duration_ms is not None
    assert timings.provider_ms == pytest.approx(50.0)
    assert timings.ttft_ms is None


def test_observe_non_streaming_without_started_at_is_a_no_op():
    """A request that bypassed the middleware must not raise."""
    timings = observe_non_streaming(
        _FakeRequest(), model="m-obs-2", path="provider", provider_ms=50.0
    )
    assert timings == LatencyTimings(
        duration_ms=None, provider_ms=None, ttft_ms=None
    )


def test_cache_hit_overhead_is_the_whole_duration():
    """No provider call means all elapsed time is gateway time."""
    before = _sum_for(
        metrics.gateway_overhead_seconds,
        {"model": "m-cache-hit", "path": "cache_exact"},
    ) or 0.0
    request = _FakeRequest(started_at=0.0)
    timings = observe_non_streaming(
        request, model="m-cache-hit", path="cache_exact", provider_ms=None
    )
    after = _sum_for(
        metrics.gateway_overhead_seconds,
        {"model": "m-cache-hit", "path": "cache_exact"},
    )
    assert timings.provider_ms is None
    assert after > before
    assert after - before == pytest.approx(timings.duration_ms / 1000, rel=1e-3)


def test_stream_timer_records_ttft_then_inter_token_gaps():
    timer = StreamTimer(0.0, model="m-stream")
    timer.provider_started()
    timer.delta()
    first_ttft = timer.ttft_ms
    timer.delta()
    timer.delta()
    timings = timer.finish()
    assert first_ttft is not None
    assert timings.ttft_ms == pytest.approx(first_ttft)
    assert timings.duration_ms >= timings.ttft_ms
    assert (
        _sum_for(metrics.inter_token_seconds, {"model": "m-stream"}) is not None
    )


def test_stream_timer_without_started_at_is_a_no_op():
    timer = StreamTimer(None, model="m-stream-none")
    timer.provider_started()
    timer.delta()
    assert timer.finish() == LatencyTimings(
        duration_ms=None, provider_ms=None, ttft_ms=None
    )


def test_stream_timer_with_no_deltas_leaves_ttft_none():
    """An empty completion still finishes cleanly."""
    timer = StreamTimer(0.0, model="m-stream-empty")
    timer.provider_started()
    timings = timer.finish()
    assert timings.ttft_ms is None
    assert timings.duration_ms is not None


def test_mark_sets_model_and_path_on_request_state():
    request = _FakeRequest(started_at=0.0)
    mark(request, model="m-mark", path="provider")
    assert request.state.model == "m-mark"
    assert request.state.path == "provider"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_latency.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.observability.latency'`

- [ ] **Step 3: Write the module**

Create `gatekeep/observability/latency.py`:

```python
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from gatekeep.observability.metrics import (
    gateway_overhead_seconds,
    inter_token_seconds,
    provider_duration_seconds,
    request_duration_seconds,
    ttft_seconds,
)

_SSE_CONTENT_TYPE = b"text/event-stream"


@dataclass(frozen=True)
class LatencyTimings:
    """Timing for one completed request, in milliseconds, ready for log_request.

    All three fields are None when timing was unavailable (no start stamp) or
    undefined (`provider_ms` on a cache hit, `ttft_ms` on any non-streamed
    request).
    """

    duration_ms: float | None
    provider_ms: float | None
    ttft_ms: float | None


_NO_TIMINGS = LatencyTimings(duration_ms=None, provider_ms=None, ttft_ms=None)


class LatencyMiddleware:
    """Pure ASGI middleware stamping request start and observing end-to-end latency.

    Stamps `scope["state"]["started_at"]` before any FastAPI dependency runs,
    so auth, rate limiting, and budget checks fall inside the measured window.
    Starlette backs `request.state` with `scope["state"]`, so endpoints read the
    same dict.

    On the way out it observes `request_duration_seconds`, but skips responses
    with a `text/event-stream` content type: those are self-reported by the SSE
    generator, which is the only place that knows when the last token was sent.

    It also skips any request that never published `model` and `path` onto the
    state, rather than emitting an "unknown" label. That covers both
    non-completion routes (`/healthz`, `/metrics`, `/dashboard`, static assets)
    and requests rejected before a model was resolved (validation, auth,
    rate-limit, budget, unknown prompt_name, TranslationError).

    `BaseHTTPMiddleware` is deliberately not used: it wraps the response body in
    an anyio stream and interacts awkwardly with long-lived streaming responses.

    Args:
        app: The ASGI application to wrap.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """Time one ASGI request, passing non-HTTP scopes straight through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["started_at"] = time.perf_counter()
        is_sse = False

        async def send_wrapper(message: dict) -> None:
            """Note whether the response is SSE, then forward the message."""
            nonlocal is_sse
            if message["type"] == "http.response.start":
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type" and value.lower().startswith(
                        _SSE_CONTENT_TYPE
                    ):
                        is_sse = True
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if is_sse:
            return
        model = state.get("model")
        path = state.get("path")
        if model is None or path is None:
            return
        request_duration_seconds.labels(model=model, path=path).observe(
            time.perf_counter() - state["started_at"]
        )


def mark(request: Any, *, model: str | None = None, path: str | None = None) -> None:
    """Publish the histogram labels the middleware cannot resolve on its own.

    `model` is only known after translation and possible cost-based routing;
    `path` only after the cache lookups have run. Both are written to
    `request.state`, which the middleware reads via `scope["state"]`.

    Args:
        request: The Starlette request whose state to annotate.
        model: Resolved model id, if known at this point.
        path: One of "cache_exact", "cache_semantic", "provider". Never
            "stream": the middleware skips SSE responses and the generator
            labels its own observation, so a "stream" value here would be dead
            state.
    """
    if model is not None:
        request.state.model = model
    if path is not None:
        request.state.path = path


def observe_non_streaming(
    request: Any, *, model: str, path: str, provider_ms: float | None = None
) -> LatencyTimings:
    """Observe overhead and provider histograms for a non-streamed request.

    Deliberately does not observe `request_duration_seconds`: the middleware
    already does that for non-streaming responses, and observing here too would
    double-count. The duration returned here stops at the call site (just before
    `log_request`) rather than at the end of the ASGI span, so
    `gateway_overhead_seconds` is not exactly `request_duration_seconds` minus
    `provider_duration_seconds`. The difference is sub-millisecond.

    Args:
        request: The Starlette request carrying `state.started_at`.
        model: Resolved model id, used as the metric label.
        path: One of "cache_exact", "cache_semantic", "provider".
        provider_ms: Upstream call duration, or None on a cache hit, in which
            case the entire duration is counted as gateway overhead.

    Returns:
        A LatencyTimings for log_request. All fields are None when the request
        carries no start stamp, which happens only if it bypassed the middleware.
    """
    started_at = getattr(request.state, "started_at", None)
    if started_at is None:
        return _NO_TIMINGS

    duration_ms = (time.perf_counter() - started_at) * 1000
    overhead_ms = duration_ms if provider_ms is None else duration_ms - provider_ms
    gateway_overhead_seconds.labels(model=model, path=path).observe(
        max(overhead_ms, 0.0) / 1000
    )
    if provider_ms is not None:
        provider_duration_seconds.labels(model=model).observe(provider_ms / 1000)
    return LatencyTimings(
        duration_ms=duration_ms, provider_ms=provider_ms, ttft_ms=None
    )


class StreamTimer:
    """Times one streamed completion from inside its SSE generator.

    `StreamingResponse` returns before the provider is called, so the middleware
    cannot time streaming at all; the generator has to self-report.

    Note that `provider_ms` measured here includes downstream backpressure. The
    `async for` over `provider.stream(...)` is pull-based, so a slow client
    inflates both `provider_ms` and every inter-token gap with time that is not
    the provider's. TTFT is unaffected, since the first delta arrives before any
    downstream consumption.

    Args:
        started_at: `request.state.started_at`, or None if unavailable, in which
            case every method becomes a no-op and `finish` returns all-None.
        model: Resolved model id, used as the metric label.
    """

    def __init__(self, started_at: float | None, *, model: str) -> None:
        self._started_at = started_at
        self._model = model
        self._provider_started_at: float | None = None
        self._last_delta_at: float | None = None
        self.ttft_ms: float | None = None

    def provider_started(self) -> None:
        """Mark the moment the upstream stream call begins."""
        if self._started_at is None:
            return
        self._provider_started_at = time.perf_counter()

    def delta(self) -> None:
        """Record one text delta: the first sets TTFT, the rest observe gaps."""
        if self._started_at is None:
            return
        now = time.perf_counter()
        if self._last_delta_at is None:
            self.ttft_ms = (now - self._started_at) * 1000
            ttft_seconds.labels(model=self._model).observe(self.ttft_ms / 1000)
        else:
            inter_token_seconds.labels(model=self._model).observe(
                now - self._last_delta_at
            )
        self._last_delta_at = now

    def finish(self) -> LatencyTimings:
        """Close out the stream, observe the remaining histograms, return timings.

        Returns:
            A LatencyTimings for log_request, all-None if no start stamp was
            available. `duration_ms` here genuinely is time-to-last-token, since
            log_request fires at StreamEnd.
        """
        if self._started_at is None:
            return _NO_TIMINGS

        now = time.perf_counter()
        duration_ms = (now - self._started_at) * 1000
        provider_ms = (
            None
            if self._provider_started_at is None
            else (now - self._provider_started_at) * 1000
        )
        request_duration_seconds.labels(model=self._model, path="stream").observe(
            duration_ms / 1000
        )
        overhead_ms = duration_ms if provider_ms is None else duration_ms - provider_ms
        gateway_overhead_seconds.labels(model=self._model, path="stream").observe(
            max(overhead_ms, 0.0) / 1000
        )
        if provider_ms is not None:
            provider_duration_seconds.labels(model=self._model).observe(
                provider_ms / 1000
            )
        return LatencyTimings(
            duration_ms=duration_ms, provider_ms=provider_ms, ttft_ms=self.ttft_ms
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_latency.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Register the middleware**

In `gatekeep/app.py`, add the import alongside the other `gatekeep.observability` import:

```python
from gatekeep.observability.latency import (
    LatencyMiddleware,
    StreamTimer,
    mark,
    observe_non_streaming,
)
```

Immediately after `app = FastAPI(title="gatekeep")` and before `app.include_router(dashboard_router)`:

```python
# Added first so it wraps everything: the start stamp must land before any
# FastAPI dependency (auth, rate limit, budget) runs.
app.add_middleware(LatencyMiddleware)
```

- [ ] **Step 6: Verify the app still works end to end**

Run: `pytest tests/test_endpoint.py tests/test_messages_endpoint.py -v`
Expected: PASS. Registering the middleware alone changes no behavior, because nothing publishes `model`/`path` yet, so every observation is skipped.

- [ ] **Step 7: Lint and commit**

```bash
ruff check . && ruff format .
git add gatekeep/observability/latency.py gatekeep/app.py tests/test_latency.py
git commit -m "feat(observability): add latency middleware and timing primitives"
```

---

### Task 4: Wire `/v1/chat/completions`

**Files:**
- Modify: `gatekeep/app.py` (`chat_completions` at app.py:220, `_sse` at app.py:727)
- Test: `tests/test_endpoint.py`

**Interfaces:**
- Consumes: `mark`, `observe_non_streaming`, `StreamTimer`, `LatencyTimings` from Task 3; the `log_request` kwargs from Task 2.
- Produces: nothing new. `_sse` gains a keyword-only parameter `started_at: float | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_endpoint.py`:

```python
async def test_non_streaming_records_latency_columns(client, raw_key, session):
    """Non-streamed requests get duration and provider time, but no TTFT."""
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert response.status_code == 200
    log = (await session.execute(select(RequestLog))).scalars().one()
    assert log.duration_ms is not None and log.duration_ms > 0
    assert log.provider_ms is not None and log.provider_ms >= 0
    assert log.duration_ms >= log.provider_ms
    assert log.ttft_ms is None


async def test_streaming_records_ttft_and_duration(client, raw_key, session):
    """Streamed requests get all three, with TTFT no later than the total."""
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
    assert log.ttft_ms is not None
    assert log.duration_ms is not None
    # Non-strict: a fake provider yielding without awaiting can produce equal
    # values at float resolution, and a strict < would be flaky.
    assert log.ttft_ms <= log.duration_ms
    assert log.provider_ms is not None


async def test_cache_hit_leaves_provider_ms_null(client, raw_key, session):
    """A served-from-cache response made no upstream call."""
    body = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "cache-me"}],
    }
    headers = {"Authorization": f"Bearer {raw_key}"}
    await client.post("/v1/chat/completions", headers=headers, json=body)
    await client.post("/v1/chat/completions", headers=headers, json=body)

    logs = (
        (await session.execute(select(RequestLog).order_by(RequestLog.id)))
        .scalars()
        .all()
    )
    assert len(logs) == 2
    assert logs[1].cached is True
    assert logs[1].provider_ms is None
    assert logs[1].duration_ms is not None
    assert logs[1].ttft_ms is None


async def test_middleware_does_not_record_e2e_for_sse(client, raw_key):
    """Streaming self-reports; a middleware observation would be recorded before
    the provider was even called."""
    from gatekeep.observability import metrics

    def sum_for(path):
        for sample in metrics.request_duration_seconds.collect()[0].samples:
            if sample.name.endswith("_sum") and sample.labels == {
                "model": "claude-sonnet-5",
                "path": path,
            }:
                return sample.value
        return 0.0

    before_provider = sum_for("provider")
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "sse-only"}],
            "stream": True,
        },
    ) as response:
        async for _ in response.aiter_lines():
            pass
    assert sum_for("provider") == before_provider
    assert sum_for("stream") > 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_endpoint.py -k latency -v; pytest tests/test_endpoint.py -k "records_ttft or provider_ms_null or does_not_record_e2e or records_latency" -v`
Expected: FAIL with `assert None is not None` on `log.duration_ms`.

- [ ] **Step 3: Wire the non-streaming path**

In `gatekeep/app.py`, change the `chat_completions` signature to add `request: Request` as the first parameter:

```python
async def chat_completions(
    request: Request,
    req: ChatCompletionRequest,
    key: ApiKey = Depends(require_budget),
    session: AsyncSession = Depends(get_session),
):
```

After the routing block, right after `requests_total.labels(...).inc()` (app.py:295):

```python
    mark(request, model=model)
```

In the streaming branch, pass the start stamp into the generator:

```python
    if req.stream:
        return StreamingResponse(
            _sse(
                provider,
                payload,
                model,
                key_id=key.id,
                prompt_name=req.prompt_name,
                routed_from=routed_from,
                prompt_version_num=served_prompt_version,
                started_at=getattr(request.state, "started_at", None),
            ),
            media_type="text/event-stream",
        )
```

In the exact-cache-hit branch, immediately before `await log_request(`:

```python
        mark(request, path="cache_exact")
        timings = observe_non_streaming(
            request, model=model, path="cache_exact", provider_ms=None
        )
```

and add to that `log_request(...)` call:

```python
            duration_ms=timings.duration_ms,
            provider_ms=timings.provider_ms,
            ttft_ms=timings.ttft_ms,
```

In the semantic-cache-hit branch, immediately before its `await log_request(`:

```python
            mark(request, path="cache_semantic")
            timings = observe_non_streaming(
                request, model=model, path="cache_semantic", provider_ms=None
            )
```

and add the same three kwargs to that `log_request(...)` call.

Replace the provider call block (app.py:389-392) with:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path="provider")
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

Immediately before the final `await log_request(`:

```python
    timings = observe_non_streaming(
        request, model=model, path="provider", provider_ms=provider_ms
    )
```

and add the same three kwargs to that `log_request(...)` call.

- [ ] **Step 4: Wire the streaming path**

Change the `_sse` signature to add a keyword-only parameter:

```python
async def _sse(
    provider: _GatewayProvider,
    payload: dict,
    model: str,
    *,
    key_id: int,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    started_at: float | None = None,
):
```

Add to its docstring:

```
    `started_at` is the middleware's start stamp, passed in because a
    StreamingResponse is returned before the provider is ever called, so the
    middleware cannot time this path itself. Timing is recorded via StreamTimer
    and lands on the same RequestLog row.
```

After `created = int(time.time())` and before the first `yield`:

```python
    timer = StreamTimer(started_at, model=model)
```

Immediately before `async for ev in provider.stream(payload):`:

```python
    timer.provider_started()
```

In the `TextDelta` branch, as the first statement:

```python
                timer.delta()
```

In the `StreamEnd` branch, immediately before `async with SessionLocal() as session:`:

```python
                timings = timer.finish()
```

and add to that `log_request(...)` call:

```python
                        duration_ms=timings.duration_ms,
                        provider_ms=timings.provider_ms,
                        ttft_ms=timings.ttft_ms,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_endpoint.py -v`
Expected: PASS, including the four new tests and all pre-existing ones.

- [ ] **Step 6: Lint and commit**

```bash
ruff check . && ruff format .
git add gatekeep/app.py tests/test_endpoint.py
git commit -m "feat(app): record latency on /v1/chat/completions"
```

---

### Task 5: Wire `/v1/messages`

**Files:**
- Modify: `gatekeep/app.py` (`messages` at app.py:449, `_messages_sse` at app.py:655)
- Test: `tests/test_messages_endpoint.py`

**Interfaces:**
- Consumes: exactly what Task 4 consumed. `_messages_sse` gains `started_at: float | None` keyword-only, same as `_sse`.
- Produces: nothing new.

The two endpoints are structurally parallel, so this task mirrors Task 4. The code is repeated in full rather than cross-referenced, because the two functions differ in their response construction and in which error mapper they call.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_messages_endpoint.py`. If `RequestLog` and `select` are not already imported there, add `from sqlalchemy import select` and add `RequestLog` to the `gatekeep.models` import.

```python
async def test_messages_non_streaming_records_latency(client, raw_key, session):
    """The Anthropic-native endpoint records the same columns as the OpenAI one."""
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
    assert log.duration_ms is not None and log.duration_ms > 0
    assert log.provider_ms is not None
    assert log.duration_ms >= log.provider_ms
    assert log.ttft_ms is None


async def test_messages_streaming_records_ttft(client, raw_key, session):
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
    assert log.ttft_ms is not None
    assert log.duration_ms is not None
    assert log.ttft_ms <= log.duration_ms
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_messages_endpoint.py -k "records_latency or records_ttft" -v`
Expected: FAIL with `assert None is not None`.

- [ ] **Step 3: Wire the non-streaming path**

Change the `messages` signature to add `request: Request` first:

```python
async def messages(
    request: Request,
    req: MessagesRequest,
    key: ApiKey = Depends(require_budget),
    session: AsyncSession = Depends(get_session),
):
```

After `requests_total.labels(...).inc()` (app.py:499):

```python
    mark(request, model=model)
```

In the streaming branch, add to the `_messages_sse(...)` call:

```python
                started_at=getattr(request.state, "started_at", None),
```

In the exact-cache-hit branch, immediately before `await log_request(`:

```python
        mark(request, path="cache_exact")
        timings = observe_non_streaming(
            request, model=model, path="cache_exact", provider_ms=None
        )
```

and add to that `log_request(...)` call:

```python
            duration_ms=timings.duration_ms,
            provider_ms=timings.provider_ms,
            ttft_ms=timings.ttft_ms,
```

In the semantic-cache-hit branch, immediately before its `await log_request(`:

```python
            mark(request, path="cache_semantic")
            timings = observe_non_streaming(
                request, model=model, path="cache_semantic", provider_ms=None
            )
```

and add the same three kwargs to that `log_request(...)` call.

Replace the provider call block (app.py:595-598) with:

```python
    # Marked before the call so a provider error still carries labels.
    mark(request, path="provider")
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error_anthropic(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
```

Immediately before the final `await log_request(`:

```python
    timings = observe_non_streaming(
        request, model=model, path="provider", provider_ms=provider_ms
    )
```

and add the same three kwargs to that `log_request(...)` call.

- [ ] **Step 4: Wire the streaming path**

Change the `_messages_sse` signature to add a keyword-only parameter:

```python
    started_at: float | None = None,
```

Add to its docstring:

```
    `started_at` is the middleware's start stamp, passed in because a
    StreamingResponse is returned before the provider is ever called, so the
    middleware cannot time this path itself.
```

Before the first `yield` in the body:

```python
    timer = StreamTimer(started_at, model=model)
```

Immediately before `async for ev in provider.stream(payload):`:

```python
    timer.provider_started()
```

As the first statement of the `TextDelta` branch:

```python
                timer.delta()
```

In the `StreamEnd` branch, immediately before the `log_request` call's surrounding session block:

```python
                timings = timer.finish()
```

and add to that `log_request(...)` call:

```python
                        duration_ms=timings.duration_ms,
                        provider_ms=timings.provider_ms,
                        ttft_ms=timings.ttft_ms,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_messages_endpoint.py -v`
Expected: PASS.

- [ ] **Step 6: Lint and commit**

```bash
ruff check . && ruff format .
git add gatekeep/app.py tests/test_messages_endpoint.py
git commit -m "feat(app): record latency on /v1/messages"
```

---

### Task 6: Edge cases and full-suite verification

**Files:**
- Test: `tests/test_latency.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing.

- [ ] **Step 1: Pin the derived-ITL formula in an executable test**

This step is deliberately **not** red-green TDD. Mean inter-token latency is a
derived read-time formula with no production code in this phase (the dashboard
that will use it is out of scope). The point is to pin the guard so the Phase 2
implementer inherits a test rather than rediscovering the divide-by-zero.

Append to `tests/test_latency.py`:

```python
# -- derived mean inter-token latency ----------------------------------
#
# No production code computes this yet: it is a read-time formula for the
# out-of-scope dashboard. Pinned here so the guard is inherited, not
# rediscovered. The SQL equivalent is
# (duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0).


def mean_itl_ms(
    duration_ms: float, ttft_ms: float, completion_tokens: int
) -> float | None:
    """Mean inter-token latency in ms, or None when it is undefined.

    Undefined below two completion tokens: one token has no gap to measure, and
    OllamaProvider.stream yields `eval_count or 0`, so zero tokens is reachable
    and would otherwise produce a denominator of -1 and a negative result.

    Args:
        duration_ms: Total streamed request duration.
        ttft_ms: Time to first token.
        completion_tokens: Tokens the provider reported generating.

    Returns:
        Mean ms between tokens, or None when completion_tokens < 2.
    """
    if completion_tokens < 2:
        return None
    return (duration_ms - ttft_ms) / (completion_tokens - 1)


def test_mean_itl_is_undefined_below_two_tokens():
    assert mean_itl_ms(100.0, 10.0, 0) is None
    assert mean_itl_ms(100.0, 10.0, 1) is None


def test_mean_itl_is_positive_for_normal_completions():
    assert mean_itl_ms(100.0, 10.0, 10) == pytest.approx(10.0)
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `pytest tests/test_latency.py -k mean_itl -v`
Expected: PASS (2 tests).

- [ ] **Step 3: Confirm the whole suite is green**

Run: `pytest -v`
Expected: PASS with no failures and no new warnings. Investigate and fix anything that fails, including pre-existing flakiness.

- [ ] **Step 4: Document the metrics**

`README.md` has no per-metric list today, only a one-paragraph description of
`/metrics` at README.md:151. Add the following immediately after that
paragraph:

```markdown
Latency metrics:

- `gatekeep_request_duration_seconds{model,path}` - end-to-end latency.
  **Pin `path` when querying.** For `path="stream"` this is start until the
  last token; for every other path it is the full request span. Aggregating
  across paths mixes two different definitions.
- `gatekeep_provider_duration_seconds{model}` - time in the upstream call. On
  the streaming path this includes downstream backpressure, since the stream
  loop is pull-based, so it is not comparable like-for-like with the
  non-streaming figure.
- `gatekeep_gateway_overhead_seconds{model,path}` - request time not spent
  upstream. On a cache hit this is the entire duration.
- `gatekeep_ttft_seconds{model}` - time to first token, streaming only.
- `gatekeep_inter_token_seconds{model}` - gap between streamed deltas. This is
  really inter-*chunk* latency: providers do not guarantee one token per delta.
  The token-normalized figure is derived from `request_logs` instead, as
  `(duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0)`, which is
  undefined below two completion tokens.

Per-request latency is also stored on `request_logs` as `duration_ms`,
`provider_ms`, and `ttft_ms`. `provider_ms` is NULL on a cache hit and
`ttft_ms` is NULL on any non-streamed request. A NULL `provider_ms` alone
cannot distinguish a cache hit from a row predating the migration - filter on
`cached`.
```

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && ruff format .
git add tests/test_latency.py README.md
git commit -m "test(observability): cover derived ITL edge cases; document latency metrics"
```

---

## Out of scope

Per the spec, this plan deliberately does **not** include:

- Any dashboard endpoint or UI panel. The columns are populated; surfacing them is a follow-up designed against real recorded data.
- Per-segment decomposition of gateway overhead (embedding vs. pgvector vs. Redis vs. DB commit).
- Latency as an input to `route_by_cost`.
- Changes to the labels of any existing metric.
