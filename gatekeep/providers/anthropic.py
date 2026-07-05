from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


@dataclass
class TextDelta:
    text: str


@dataclass
class StreamEnd:
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
        async with self._client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                yield TextDelta(text=text)
            final = await stream.get_final_message()
            yield StreamEnd(
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )
