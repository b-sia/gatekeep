from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_LATENCY_MS = 100.0
DEFAULT_OUTPUT_TOKENS = 100

_SEGMENT_RE = re.compile(r"^(lat|out|itl)(\d+)$")


@dataclass(frozen=True)
class StubParams:
    """Parsed load-test parameters encoded in a stub model string.

    Attributes:
        latency_ms: Delay before the first (and, for a non-streaming call,
            only) response - drives time-to-first-token in streaming.
        output_tokens: Number of canned output tokens to generate.
        itl_ms: Delay between successive streamed text deltas.
    """

    latency_ms: float
    output_tokens: int
    itl_ms: float


def parse_stub_model(model: str) -> StubParams:
    """Parse a stub model id (already stripped of its `stub/` prefix by
    `resolve_route`) into `StubParams`.

    Recognizes hyphen-separated `lat<ms>`, `out<tokens>`, `itl<ms>` segments
    in any order, e.g. `"lat50-out200-itl5"`. Parsing is total and forgiving:
    `""`, `"default"`, and any unparseable or partially-parseable suffix
    (unknown segments are skipped, not fatal) fall back to documented
    defaults rather than raising, so a load script never fails on a typo
    mid-run.

    When `itl_ms` is not given, it defaults to `latency_ms / output_tokens`
    (0 if `output_tokens` is 0) - scenarios with a bigger configured latency
    also get a slower per-token cadence by default, without needing to spell
    out all three segments.

    Args:
        model: The stub model id, with any `stub/` prefix already removed.

    Returns:
        The parsed (or defaulted) `StubParams`.
    """
    latency_ms: float | None = None
    output_tokens: int | None = None
    itl_ms: float | None = None
    if model and model != "default":
        for segment in model.split("-"):
            match = _SEGMENT_RE.match(segment)
            if match is None:
                continue
            kind, raw_value = match.group(1), int(match.group(2))
            if kind == "lat":
                latency_ms = float(raw_value)
            elif kind == "out":
                output_tokens = raw_value
            elif kind == "itl":
                itl_ms = float(raw_value)
    latency_ms = DEFAULT_LATENCY_MS if latency_ms is None else latency_ms
    output_tokens = DEFAULT_OUTPUT_TOKENS if output_tokens is None else output_tokens
    if itl_ms is None:
        itl_ms = latency_ms / output_tokens if output_tokens else 0.0
    return StubParams(latency_ms=latency_ms, output_tokens=output_tokens, itl_ms=itl_ms)
