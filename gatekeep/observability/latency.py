from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from gatekeep.observability.metrics import (
    gateway_overhead_seconds,
    inter_token_seconds,
    provider_duration_seconds,
    request_duration_seconds,
    time_to_last_token_seconds,
    ttft_seconds,
)


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


# Sentinel distinguishing "provider_ms not published" (mark() leaves the state
# key untouched) from "published as None" (a cache hit: no provider call, so
# the entire span counts as overhead). Both read back as None from
# `state.get("provider_ms")` and are handled identically by the middleware,
# but the sentinel is what lets `mark()` keep its usual "None means leave
# alone" convention for model/path while still letting provider_ms be set to
# None on purpose.
_UNSET = object()


class LatencyMiddleware:
    """Pure ASGI middleware stamping request start and observing end-to-end latency.

    Stamps `scope["state"]["started_at"]` before any FastAPI dependency runs,
    so auth, rate limiting, and budget checks fall inside the measured window.
    Starlette backs `request.state` with `scope["state"]`, so endpoints read the
    same dict.

    On the way out it observes `request_duration_seconds` and
    `gateway_overhead_seconds` for every request, streaming included: the ASGI
    call does not return until the response body is fully sent, so the
    measured span covers the whole stream. This is the only writer of either
    histogram, which is what lets both carry a single definition of "overhead"
    and "end-to-end" across all `path` values - `gateway_overhead_seconds` is
    computed from the exact same span as `request_duration_seconds`, so
    `overhead = duration - provider` holds by construction, not just on
    average.

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

        await self.app(scope, receive, send)

        model = state.get("model")
        path = state.get("path")
        if model is None or path is None:
            return
        e2e_seconds = time.perf_counter() - state["started_at"]
        request_duration_seconds.labels(model=model, path=path).observe(e2e_seconds)

        provider_ms = state.get("provider_ms", _UNSET)
        if provider_ms is _UNSET:
            # path was marked (so model/path are set) but nothing ever
            # published provider_ms - e.g. the provider call raised before
            # observe_non_streaming/StreamTimer.finish() ran. We can't tell
            # how much of e2e_seconds was provider time, so skip rather than
            # miscount the whole span as gateway overhead.
            return
        overhead_seconds = (
            e2e_seconds if provider_ms is None else e2e_seconds - provider_ms / 1000
        )
        gateway_overhead_seconds.labels(model=model, path=path).observe(
            max(overhead_seconds, 0.0)
        )


def mark(
    request: Any,
    *,
    model: str | None = None,
    path: str | None = None,
    provider_ms: float | None | object = _UNSET,
) -> None:
    """Publish the histogram labels the middleware cannot resolve on its own.

    `model` is only known after translation and possible cost-based routing;
    `path` only after the cache lookups have run; `provider_ms` only once the
    upstream call (or cache lookup) has completed. All are written to
    `request.state`, which the middleware reads via `scope["state"]`.

    Args:
        request: The Starlette request whose state to annotate.
        model: Resolved model id, if known at this point.
        path: One of "cache_exact", "cache_semantic", "provider", "stream".
            The streaming endpoints set "stream" before returning their
            StreamingResponse, since the middleware observes that request too
            and cannot tell from the scope which branch produced it.
        provider_ms: Upstream call duration, feeding the middleware's
            `gateway_overhead_seconds` observation. Pass None explicitly (as
            the cache paths do) to mark a request as having no provider call,
            so the middleware counts the entire span as overhead. Leaving
            this unset (the default) does not touch `state["provider_ms"]`,
            distinct from passing None.
    """
    if model is not None:
        request.state.model = model
    if path is not None:
        request.state.path = path
    if provider_ms is not _UNSET:
        request.state.provider_ms = provider_ms


def observe_non_streaming(
    request: Any,
    *,
    model: str,
    path: str,
    provider_ms: float | None = None,
    count_latency: bool = True,
) -> LatencyTimings:
    """Observe the provider-duration histogram and publish overhead inputs
    for a non-streamed request.

    Deliberately does not observe `request_duration_seconds` or
    `gateway_overhead_seconds` directly: the middleware owns both, and
    observing here too would double-count. Instead this publishes
    `provider_ms` onto `request.state` via `mark()` so the middleware can
    derive overhead from its own end-to-end span once the response closes.

    Args:
        request: The Starlette request carrying `state.started_at`.
        model: Resolved model id, used as the metric label.
        path: One of "cache_exact", "cache_semantic", "provider".
        provider_ms: Upstream call duration, or None on a cache hit, in which
            case the entire duration is counted as gateway overhead.
        count_latency: Whether to observe the `provider_duration_seconds`
            histogram. Failed requests set this False: `provider_ms` is still
            marked so the middleware can attribute overhead, but a failed
            call's duration must not enter the provider-latency histogram,
            matching the exclusion of failed rows from the DB latency
            percentiles.

    Returns:
        A LatencyTimings for log_request. All fields are None when the request
        carries no start stamp, which happens only if it bypassed the middleware.
    """
    started_at = getattr(request.state, "started_at", None)
    if started_at is None:
        return _NO_TIMINGS

    duration_ms = (time.perf_counter() - started_at) * 1000
    mark(request, provider_ms=provider_ms)
    if provider_ms is not None and count_latency:
        provider_duration_seconds.labels(model=model).observe(provider_ms / 1000)
    return LatencyTimings(
        duration_ms=duration_ms, provider_ms=provider_ms, ttft_ms=None
    )


class StreamTimer:
    """Times one streamed completion from inside its SSE generator.

    The middleware measures the full ASGI span for streamed requests as it does
    for any other, so end-to-end is not this class's job. What only the
    generator can see is where the tokens land inside that span: TTFT, the gaps
    between deltas, and time-to-last-token, which stops at the final delta
    rather than at the end of the response body.

    Note that `provider_ms` measured here includes downstream backpressure. The
    `async for` over `provider.stream(...)` is pull-based, so a slow client
    inflates both `provider_ms` and every inter-token gap with time that is not
    the provider's. TTFT is unaffected, since the first delta arrives before any
    downstream consumption.

    `finish()` publishes `provider_ms` onto `state["provider_ms"]` rather than
    observing `gateway_overhead_seconds` itself: the generator has no
    `request` to call `mark()` on (it outlives the endpoint that returned it),
    but `state` is the same `scope["state"]` dict the middleware reads back
    once the stream closes, so writing into it here is equivalent.

    Args:
        state: `request.scope["state"]`, or None if unavailable, in which case
            every method becomes a no-op and `finish` returns all-None. Reused
            rather than a bare `started_at` float so `finish()` has somewhere
            to publish `provider_ms` back to the middleware.
        model: Resolved model id, used as the metric label.
    """

    def __init__(self, state: dict | None, *, model: str) -> None:
        self._state = state
        self._started_at = None if state is None else state.get("started_at")
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

    def finish(self, *, succeeded: bool = True) -> LatencyTimings:
        """Close out the stream, observe the remaining histograms, return timings.

        `succeeded=False` (a mid-stream provider error or client disconnect)
        changes which reference point `duration_ms` uses: the last delta
        actually emitted, not the failure moment, so a hung failed stream
        doesn't inflate `duration_ms` with dead wait time it spent doing
        nothing useful. `duration_ms` is None on a failed stream if no delta
        ever arrived. `time_to_last_token_seconds` is only observed on a
        clean finish, matching the DB-side exclusion of failed rows from
        percentiles (see gatekeep/api/dashboard.py's `_latency_filters`).
        `provider_ms` is measured to the failure moment either way (real
        upstream time spent, including the failed wait) and is always
        published onto `state` so the middleware's overhead calculation
        still runs.

        Args:
            succeeded: Whether the stream reached a clean StreamEnd. Defaults
                to True, preserving the pre-existing behavior for any
                positional `finish()` call.

        Returns:
            A LatencyTimings for log_request, all-None if no start stamp was
            available.
        """
        if self._started_at is None:
            return _NO_TIMINGS

        now = time.perf_counter()
        provider_ms = (
            None
            if self._provider_started_at is None
            else (now - self._provider_started_at) * 1000
        )
        if succeeded:
            duration_ms = (now - self._started_at) * 1000
            time_to_last_token_seconds.labels(model=self._model).observe(
                duration_ms / 1000
            )
        else:
            duration_ms = (
                None
                if self._last_delta_at is None
                else (self._last_delta_at - self._started_at) * 1000
            )
        # self._state is never None here: self._started_at is only set from
        # state.get(...), so a non-None started_at implies a non-None state.
        self._state["provider_ms"] = provider_ms
        if provider_ms is not None:
            provider_duration_seconds.labels(model=self._model).observe(
                provider_ms / 1000
            )
        return LatencyTimings(
            duration_ms=duration_ms, provider_ms=provider_ms, ttft_ms=self.ttft_ms
        )
