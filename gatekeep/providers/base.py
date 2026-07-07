from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CompletionResult:
    """A normalized, SDK-independent result from a non-streaming completion call."""

    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


@dataclass
class TextDelta:
    """One incremental text chunk emitted while streaming a completion."""

    text: str


@dataclass
class StreamEnd:
    """The terminal event of a streamed completion, carrying final usage and stop reason."""

    stop_reason: str | None
    input_tokens: int
    output_tokens: int
