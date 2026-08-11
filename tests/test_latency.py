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
    timings = observe_non_streaming(request, model="m-obs-1", path="provider", provider_ms=50.0)
    assert timings.duration_ms is not None
    assert timings.provider_ms == pytest.approx(50.0)
    assert timings.ttft_ms is None


def test_observe_non_streaming_without_started_at_is_a_no_op():
    """A request that bypassed the middleware must not raise."""
    timings = observe_non_streaming(
        _FakeRequest(), model="m-obs-2", path="provider", provider_ms=50.0
    )
    assert timings == LatencyTimings(duration_ms=None, provider_ms=None, ttft_ms=None)


def test_observe_non_streaming_cache_hit_publishes_provider_ms_none():
    """No provider call: provider_ms is published as None (not left unset),
    so the middleware treats the whole span as overhead rather than skipping
    the observation."""
    request = _FakeRequest(started_at=0.0)
    timings = observe_non_streaming(
        request, model="m-cache-hit", path="cache_exact", provider_ms=None
    )
    assert timings.provider_ms is None
    assert request.state.provider_ms is None


def test_observe_non_streaming_publishes_provider_ms_for_middleware():
    """Overhead is no longer observed here: the middleware derives it from
    its own end-to-end span, fed by provider_ms published onto request.state."""
    before = _sum_for(
        metrics.gateway_overhead_seconds,
        {"model": "m-obs-1", "path": "provider"},
    )
    request = _FakeRequest(started_at=0.0)
    timings = observe_non_streaming(request, model="m-obs-1", path="provider", provider_ms=50.0)
    after = _sum_for(
        metrics.gateway_overhead_seconds,
        {"model": "m-obs-1", "path": "provider"},
    )
    assert request.state.provider_ms == pytest.approx(50.0)
    assert after == before, "observe_non_streaming must not write this histogram itself"
    assert timings.provider_ms == pytest.approx(50.0)


def test_stream_timer_records_ttft_then_inter_token_gaps():
    timer = StreamTimer({"started_at": 0.0}, model="m-stream")
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


def test_stream_timer_records_time_to_last_token_not_request_duration():
    """E2E for streams is the middleware's job now; the timer owns TTLT."""
    e2e_labels = {"model": "m-ttlt", "path": "stream"}
    before_e2e = _sum_for(metrics.request_duration_seconds, e2e_labels)
    before_ttlt = _sum_for(metrics.time_to_last_token_seconds, {"model": "m-ttlt"}) or 0.0

    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-ttlt")
    timer.provider_started()
    timer.delta()
    timings = timer.finish()

    assert _sum_for(metrics.request_duration_seconds, e2e_labels) == before_e2e
    after_ttlt = _sum_for(metrics.time_to_last_token_seconds, {"model": "m-ttlt"})
    assert after_ttlt - before_ttlt == pytest.approx(timings.duration_ms / 1000, rel=1e-3)


def test_stream_timer_publishes_provider_ms_onto_state_for_the_middleware():
    """finish() writes provider_ms back onto the shared scope-state dict rather
    than observing gateway_overhead_seconds itself: the generator has no
    request to call mark() on."""
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-ttlt-state")
    timer.provider_started()
    timings = timer.finish()

    assert timings.provider_ms is not None
    assert state["provider_ms"] == pytest.approx(timings.provider_ms)
    assert (
        _sum_for(
            metrics.gateway_overhead_seconds,
            {"model": "m-ttlt-state", "path": "stream"},
        )
        is None
    ), "StreamTimer must not write this histogram itself anymore"


def test_stream_timer_without_started_at_is_a_no_op():
    timer = StreamTimer(None, model="m-stream-none")
    timer.provider_started()
    timer.delta()
    assert timer.finish() == LatencyTimings(duration_ms=None, provider_ms=None, ttft_ms=None)


def test_stream_timer_with_no_deltas_leaves_ttft_none():
    """An empty completion still finishes cleanly."""
    timer = StreamTimer({"started_at": 0.0}, model="m-stream-empty")
    timer.provider_started()
    timings = timer.finish()
    assert timings.ttft_ms is None
    assert timings.duration_ms is not None


def test_stream_timer_finish_failed_uses_last_delta_as_duration_reference():
    """A failed stream's duration_ms is time-to-last-token, not
    time-to-failure: the gap between the last delta and the failure moment
    must not inflate duration_ms."""
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-1")
    timer.provider_started()
    timer.delta()
    last_delta_at = timer._last_delta_at
    timings = timer.finish(succeeded=False)
    assert timings.duration_ms == pytest.approx((last_delta_at - 0.0) * 1000)


def test_stream_timer_finish_failed_before_any_token_has_null_duration():
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-2")
    timer.provider_started()
    timings = timer.finish(succeeded=False)
    assert timings.duration_ms is None
    assert timings.ttft_ms is None


def test_stream_timer_finish_failed_still_publishes_provider_ms():
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-3")
    timer.provider_started()
    timer.delta()
    timings = timer.finish(succeeded=False)
    assert timings.provider_ms is not None
    assert state["provider_ms"] == pytest.approx(timings.provider_ms)


def test_stream_timer_finish_failed_does_not_observe_time_to_last_token():
    ttlt_labels = {"model": "m-fail-4"}
    before = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-fail-4")
    timer.provider_started()
    timer.delta()
    timer.finish(succeeded=False)
    after = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    assert after == before


def test_stream_timer_finish_succeeded_default_is_unchanged():
    """succeeded defaults to True so every pre-existing call site
    (positional `timer.finish()`) keeps its current behavior."""
    ttlt_labels = {"model": "m-succeed-default"}
    before = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    state = {"started_at": 0.0}
    timer = StreamTimer(state, model="m-succeed-default")
    timer.provider_started()
    timer.delta()
    timings = timer.finish()
    after = _sum_for(metrics.time_to_last_token_seconds, ttlt_labels) or 0.0
    assert after > before
    assert timings.duration_ms is not None


def test_mark_sets_model_and_path_on_request_state():
    request = _FakeRequest(started_at=0.0)
    mark(request, model="m-mark", path="provider")
    assert request.state.model == "m-mark"
    assert request.state.path == "provider"


def test_mark_provider_ms_none_is_distinct_from_unset():
    """Passing provider_ms=None must still publish it (a cache hit), while
    leaving it out entirely must not touch state at all."""
    request = _FakeRequest(started_at=0.0)
    mark(request, model="m-mark-2")
    assert not hasattr(request.state, "provider_ms")

    mark(request, provider_ms=None)
    assert request.state.provider_ms is None

    mark(request, provider_ms=42.0)
    assert request.state.provider_ms == pytest.approx(42.0)


# -- derived mean inter-token latency ----------------------------------
#
# No production code computes this yet: it is a read-time formula for the
# out-of-scope dashboard. Pinned here so the guard is inherited, not
# rediscovered. The SQL equivalent is
# (duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0).


def mean_itl_ms(duration_ms: float, ttft_ms: float, completion_tokens: int) -> float | None:
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
