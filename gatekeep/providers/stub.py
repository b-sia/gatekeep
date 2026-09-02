from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gatekeep.accounts.accounting import estimate_tokens
from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta

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


def _canned_text(output_tokens: int) -> str:
    """Deterministic canned text sized to roughly 4 characters per token,
    matching the heuristic `accounting.estimate_tokens` uses elsewhere."""
    if output_tokens <= 0:
        return ""
    word = "gatekeep-stub "
    target_chars = output_tokens * 4
    repeated = (word * (target_chars // len(word) + 1))[:target_chars]
    return repeated.strip()


def _chunk_text(text: str, n: int) -> list[str]:
    """Split `text` into at most `n` roughly equal, order-preserving pieces."""
    if n <= 0 or not text:
        return []
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)][:n]


def _payload_text(payload: dict[str, Any]) -> str:
    """Concatenate a payload's system prompt and message contents, for
    estimating a plausible input-token count."""
    parts = [payload.get("system") or ""]
    parts.extend(m.get("content", "") for m in payload.get("messages", []))
    return "\n".join(parts)


class StubProvider:
    """Zero-cost, deterministic provider for load testing gatekeep's own
    request-handling overhead. Never calls a real upstream - every
    parameter (latency, output size, inter-token delay) is decoded from the
    model string by `parse_stub_model`. See
    docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §1.
    """

    def __init__(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Args:
        sleep: Injected async sleep, real `asyncio.sleep` by default.
            Tests pass a fake to assert durations without waiting.
        """
        self._sleep = sleep

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Sleep for the parsed latency, then return a canned, sized result."""
        params = parse_stub_model(payload["model"])
        await self._sleep(params.latency_ms / 1000)
        return CompletionResult(
            text=_canned_text(params.output_tokens),
            input_tokens=estimate_tokens(_payload_text(payload)),
            output_tokens=params.output_tokens,
            stop_reason="stop",
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        """Sleep once for TTFT, then yield sized deltas spaced by the parsed
        inter-token delay, followed by a terminal `StreamEnd`."""
        params = parse_stub_model(payload["model"])
        await self._sleep(params.latency_ms / 1000)
        chunks = _chunk_text(_canned_text(params.output_tokens), params.output_tokens)
        for i, chunk in enumerate(chunks):
            yield TextDelta(text=chunk)
            if i < len(chunks) - 1:
                await self._sleep(params.itl_ms / 1000)
        yield StreamEnd(
            stop_reason="stop",
            input_tokens=estimate_tokens(_payload_text(payload)),
            output_tokens=params.output_tokens,
        )
