from __future__ import annotations

import uuid
from typing import Any, Literal

from gatekeep.api.anthropic_schemas import (
    ContentBlock,
    MessagesRequest,
    MessagesResponse,
    MessagesUsage,
)
from gatekeep.api.openai_schemas import ChatCompletionResponse
from gatekeep.api.translation import extract_text, resolve_route

# CompletionResult.stop_reason (and a cached ChatCompletionResponse's
# choices[0].finish_reason) are always OpenAI-canonical vocabulary - every
# provider normalizes to it (see providers/*.py _FINISH_REASON_MAP). This is
# the reverse mapping back to Anthropic's own vocabulary for the native
# /v1/messages surface. Anthropic's "stop_sequence" reason has no
# OpenAI-canonical counterpart - both it and "end_turn" already collapse to
# "stop" upstream, so a stop-sequence hit is reported here as "end_turn".
# Disambiguating that would mean carrying a richer stop_reason through
# CompletionResult for all four providers; out of scope for this passthrough.
_REVERSE_FINISH_REASON_MAP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}


def reverse_finish_reason(reason: str | None) -> str:
    """Map an OpenAI-canonical stop reason back to Anthropic's vocabulary."""
    return _REVERSE_FINISH_REASON_MAP.get(reason or "stop", "end_turn")


def new_message_id() -> str:
    """Generate a fresh Anthropic-style `msg_...` message ID."""
    return "msg_" + uuid.uuid4().hex


def messages_to_payload(
    req: MessagesRequest, *, model_aliases: dict[str, str]
) -> tuple[Literal["anthropic", "ollama", "openai", "google"], dict[str, Any]]:
    """Build a provider-neutral completion payload from a Messages API request.

    Unlike `openai_to_payload`, the Messages API already separates `system`
    from `messages`, so there's no lifting to do - only text extraction (a
    content-block list collapses to plain text, matching v1's text-only
    scope) and provider/model resolution via the shared `resolve_route`.
    """
    messages = [
        {"role": m.role, "content": extract_text(m.content)} for m in req.messages
    ]
    provider, model = resolve_route(req.model, aliases=model_aliases)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens,
    }
    system_text = extract_text(req.system) if req.system is not None else ""
    if system_text:
        payload["system"] = system_text
    if req.stop_sequences:
        payload["stop_sequences"] = req.stop_sequences
    return provider, payload


def result_to_messages(result: Any, *, model: str) -> MessagesResponse:
    """Convert a normalized provider CompletionResult into a Messages API response."""
    return MessagesResponse(
        id=new_message_id(),
        model=model,
        content=[ContentBlock(text=result.text)],
        stop_reason=reverse_finish_reason(result.stop_reason),
        usage=MessagesUsage(
            input_tokens=result.input_tokens, output_tokens=result.output_tokens
        ),
    )


def openai_response_to_messages(cached: ChatCompletionResponse) -> MessagesResponse:
    """Convert a cached OpenAI-shaped response (the cache's only storage format) to Messages shape.

    Lets `/v1/messages` share the exact/semantic cache with
    `/v1/chat/completions` - the cache key is payload-derived and
    provider-neutral, so a hit written by one endpoint is a valid hit read
    by the other.
    """
    choice = cached.choices[0]
    return MessagesResponse(
        id=new_message_id(),
        model=cached.model,
        content=[ContentBlock(text=choice.message.content or "")],
        stop_reason=reverse_finish_reason(choice.finish_reason),
        usage=MessagesUsage(
            input_tokens=cached.usage.prompt_tokens,
            output_tokens=cached.usage.completion_tokens,
        ),
    )


def message_start_event(*, id: str, model: str) -> dict[str, Any]:
    """Build the opening `message_start` event of an Anthropic-style stream.

    Token usage isn't known yet - gatekeep's provider `stream()` only
    reports usage once the stream ends - so both counts start at 0. A
    client should treat the terminal `message_delta` event's usage as
    authoritative.
    """
    return {
        "type": "message_start",
        "message": {
            "id": id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }


def content_block_start_event() -> dict[str, Any]:
    """Build the `content_block_start` event opening the (sole, text) block."""
    return {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }


def content_block_delta_event(text: str) -> dict[str, Any]:
    """Build a `content_block_delta` event carrying one text delta."""
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": text},
    }


def content_block_stop_event() -> dict[str, Any]:
    """Build the `content_block_stop` event closing the text block."""
    return {"type": "content_block_stop", "index": 0}


def message_delta_event(
    *, stop_reason: str, input_tokens: int, output_tokens: int
) -> dict[str, Any]:
    """Build the terminal `message_delta` event, carrying the authoritative usage."""
    return {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def message_stop_event() -> dict[str, Any]:
    """Build the closing `message_stop` event."""
    return {"type": "message_stop"}
