from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


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


class AnthropicProvider:
    """Thin async wrapper over the Anthropic SDK client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Send a non-streaming completion request and return a normalized result."""
        message = await self._client.messages.create(**payload)
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return CompletionResult(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        """Stream a completion, yielding text deltas followed by a final StreamEnd."""
        async with self._client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                yield TextDelta(text=text)
            final = await stream.get_final_message()
            yield StreamEnd(
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )
