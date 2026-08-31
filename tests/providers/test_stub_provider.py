from __future__ import annotations

import pytest

from gatekeep.providers.base import StreamEnd, TextDelta
from gatekeep.providers.stub import (
    DEFAULT_LATENCY_MS,
    DEFAULT_OUTPUT_TOKENS,
    StubParams,
    StubProvider,
    parse_stub_model,
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("default", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("garbage", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("lat50-out200", StubParams(50.0, 200, 0.25)),
        ("lat50-out200-itl5", StubParams(50.0, 200, 5.0)),
        # unknown/malformed segments are ignored, not fatal
        ("lat50-bogus-out200", StubParams(50.0, 200, 0.25)),
        ("out0", StubParams(DEFAULT_LATENCY_MS, 0, 0.0)),
    ],
)
def test_parse_stub_model(model, expected):
    assert parse_stub_model(model) == expected


class _RecordingSleep:
    """Fake async sleep that records requested durations instead of waiting."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _payload(model: str, content: str = "hi") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}]}


async def test_complete_sleeps_for_latency_then_returns_sized_result():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    result = await provider.complete(_payload("lat50-out20"))
    assert sleep.calls == [0.05]
    assert result.output_tokens == 20
    assert result.stop_reason == "stop"
    assert len(result.text) > 0


async def test_complete_estimates_input_tokens_from_payload():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    short = await provider.complete(_payload("default", content="hi"))
    long = await provider.complete(_payload("default", content="hi " * 100))
    assert long.input_tokens > short.input_tokens


async def test_complete_canned_text_deterministic_for_same_size():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    first = await provider.complete(_payload("out50"))
    second = await provider.complete(_payload("out50"))
    assert first.text == second.text


async def test_stream_ttft_then_itl_between_deltas_then_streamend():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    events = [event async for event in provider.stream(_payload("lat50-out4-itl5"))]
    *deltas, end = events
    assert all(isinstance(d, TextDelta) for d in deltas)
    assert len(deltas) == 4
    assert isinstance(end, StreamEnd)
    assert end.stop_reason == "stop"
    assert end.output_tokens == 4
    # TTFT sleep, then 3 inter-token sleeps between the 4 deltas (none after
    # the last delta - StreamEnd follows immediately).
    assert sleep.calls == [0.05, 0.005, 0.005, 0.005]


async def test_stream_zero_output_tokens_yields_only_streamend():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    events = [event async for event in provider.stream(_payload("out0"))]
    assert len(events) == 1
    assert isinstance(events[0], StreamEnd)
    assert events[0].output_tokens == 0
