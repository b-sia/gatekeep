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
    assert timings == LatencyTimings(duration_ms=None, provider_ms=None, ttft_ms=None)


def test_cache_hit_overhead_is_the_whole_duration():
    """No provider call means all elapsed time is gateway time."""
    before = (
        _sum_for(
            metrics.gateway_overhead_seconds,
            {"model": "m-cache-hit", "path": "cache_exact"},
        )
        or 0.0
    )
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
    assert _sum_for(metrics.inter_token_seconds, {"model": "m-stream"}) is not None


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
