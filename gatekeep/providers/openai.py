from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta

_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
}


def _to_openai_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a provider-neutral payload into OpenAI chat.completions.create() kwargs.

    Folds a `system` string into a leading system message and maps
    `stop_sequences` to OpenAI's `stop`.
    """
    messages = list(payload["messages"])
    if payload.get("system"):
        messages = [{"role": "system", "content": payload["system"]}, *messages]
    kwargs: dict[str, Any] = {
        "model": payload["model"],
        "messages": messages,
        "max_tokens": payload["max_tokens"],
    }
    if payload.get("stop_sequences"):
        kwargs["stop"] = payload["stop_sequences"]
    return kwargs


class OpenAIProvider:
    """Thin async wrapper over the OpenAI SDK client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Send a non-streaming completion request and return a normalized result."""
        response = await self._client.chat.completions.create(**_to_openai_kwargs(payload))
        choice = response.choices[0]
        return CompletionResult(
            text=choice.message.content or "",
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            stop_reason=_FINISH_REASON_MAP.get(choice.finish_reason, "stop"),
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        """Stream a completion, yielding text deltas followed by a final StreamEnd.

        Requests `stream_options={"include_usage": True}` so the final chunk
        (with an empty `choices` list) carries token usage; the preceding
        chunk with a non-null `finish_reason` carries the stop reason.
        """
        kwargs = _to_openai_kwargs(payload)
        stream = await self._client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True}
        )
        stop_reason = "stop"
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield TextDelta(text=delta.content)
                if chunk.choices[0].finish_reason:
                    stop_reason = _FINISH_REASON_MAP.get(chunk.choices[0].finish_reason, "stop")
            if chunk.usage is not None:
                yield StreamEnd(
                    stop_reason=stop_reason,
                    input_tokens=chunk.usage.prompt_tokens,
                    output_tokens=chunk.usage.completion_tokens,
                )
