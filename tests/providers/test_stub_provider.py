from __future__ import annotations

import pytest

from gatekeep.providers.stub import (
    DEFAULT_LATENCY_MS,
    DEFAULT_OUTPUT_TOKENS,
    StubParams,
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
