from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta

_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "BLOCKLIST": "content_filter",
    "LANGUAGE": "content_filter",
    "OTHER": "content_filter",
}


def _reason_name(reason: Any) -> str:
    """Normalize a google-genai FinishReason enum (or a plain string in tests) to its name."""
    return getattr(reason, "name", reason)


def _to_google_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate a provider-neutral payload into generate_content()/generate_content_stream() kwargs.

    Maps `assistant` -> `model` (Gemini's role name), folds `system` into
    `config.system_instruction`, and `max_tokens` into
    `config.max_output_tokens`.
    """
    contents = [
        {
            "role": "model" if m["role"] == "assistant" else "user",
            "parts": [{"text": m["content"]}],
        }
        for m in payload["messages"]
    ]
    config: dict[str, Any] = {"max_output_tokens": payload["max_tokens"]}
    if payload.get("system"):
        config["system_instruction"] = payload["system"]
    if payload.get("stop_sequences"):
        config["stop_sequences"] = payload["stop_sequences"]
    return {"model": payload["model"], "contents": contents, "config": config}


class GoogleProvider:
    """Thin async wrapper over the google-genai SDK client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Send a non-streaming completion request and return a normalized result."""
        response = await self._client.aio.models.generate_content(
            **_to_google_kwargs(payload)
        )
        return CompletionResult(
            text=response.text or "",
            input_tokens=response.usage_metadata.prompt_token_count or 0,
            output_tokens=response.usage_metadata.candidates_token_count or 0,
            stop_reason=_FINISH_REASON_MAP.get(
                _reason_name(response.candidates[0].finish_reason), "stop"
            ),
        )

    async def stream(
        self, payload: dict[str, Any]
    ) -> AsyncIterator[TextDelta | StreamEnd]:
        """Stream a completion, yielding text deltas followed by a final StreamEnd.

        Every chunk carries `usage_metadata` (cumulative token counts), but
        only the terminal chunk carries a populated
        `candidates[0].finish_reason` - that's the actual end-of-stream
        signal; earlier chunks carry a text delta (or nothing, which is
        skipped).
        """
        kwargs = _to_google_kwargs(payload)
        response_stream = await self._client.aio.models.generate_content_stream(**kwargs)
        async for chunk in response_stream:
            if chunk.text:
                yield TextDelta(text=chunk.text)
            finish_reason = chunk.candidates[0].finish_reason if chunk.candidates else None
            if finish_reason is not None:
                yield StreamEnd(
                    stop_reason=_FINISH_REASON_MAP.get(_reason_name(finish_reason), "stop"),
                    input_tokens=chunk.usage_metadata.prompt_token_count or 0,
                    output_tokens=chunk.usage_metadata.candidates_token_count or 0,
                )
