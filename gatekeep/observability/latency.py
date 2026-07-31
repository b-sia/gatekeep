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
