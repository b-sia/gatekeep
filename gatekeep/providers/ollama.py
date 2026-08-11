from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta

_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "stop",
    "length": "length",
}


def _to_ollama_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic-shaped payload into Ollama chat() kwargs.

    Folds a `system` string into a leading system message and maps
    `max_tokens` to Ollama's `options.num_predict`.
    """
    messages = list(payload["messages"])
    if payload.get("system"):
        messages = [{"role": "system", "content": payload["system"]}, *messages]
    kwargs: dict[str, Any] = {"model": payload["model"], "messages": messages}
    if payload.get("max_tokens") is not None:
        kwargs["options"] = {"num_predict": payload["max_tokens"]}
    return kwargs


class OllamaProvider:
    """Thin async wrapper over the Ollama client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Send a non-streaming completion request and return a normalized result."""
        response = await self._client.chat(**_to_ollama_kwargs(payload))
        return CompletionResult(
            text=response.message.content,
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
            stop_reason=_FINISH_REASON_MAP.get(response.done_reason, "stop"),
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        """Stream a completion, yielding text deltas followed by a final StreamEnd."""
        chunks = await self._client.chat(**_to_ollama_kwargs(payload), stream=True)
        async for chunk in chunks:
            if chunk.done:
                yield StreamEnd(
                    stop_reason=_FINISH_REASON_MAP.get(chunk.done_reason, "stop"),
                    input_tokens=chunk.prompt_eval_count or 0,
                    output_tokens=chunk.eval_count or 0,
                )
            elif chunk.message.content:
                yield TextDelta(text=chunk.message.content)
