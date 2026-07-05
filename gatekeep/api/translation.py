from __future__ import annotations

import time
import uuid
from typing import Any

from gatekeep.api.openai_schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    DeltaMessage,
    ResponseMessage,
    Usage,
)

FINISH_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class TranslationError(ValueError):
    """Raised when an OpenAI request cannot be mapped to Anthropic."""


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def resolve_model(requested: str, *, default_model: str, aliases: dict[str, str]) -> str:
    if requested in aliases:
        return aliases[requested]
    if requested.startswith("claude-"):
        return requested
    return default_model


def openai_to_anthropic(
    req: ChatCompletionRequest,
    *,
    default_max_tokens: int,
    default_model: str,
    model_aliases: dict[str, str],
) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for msg in req.messages:
        text = _extract_text(msg.content)
        if msg.role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": text})
        else:  # "tool" and anything else
            raise TranslationError(f"unsupported message role in v1: {msg.role}")

    if not messages:
        raise TranslationError("request must contain at least one user or assistant message")

    payload: dict[str, Any] = {
        "model": resolve_model(req.model, default_model=default_model, aliases=model_aliases),
        "messages": messages,
        "max_tokens": req.max_tokens or req.max_completion_tokens or default_max_tokens,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if req.stop:
        payload["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    # temperature/top_p/top_k intentionally omitted (rejected by Sonnet 5 / Opus 4.8).
    return payload


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def result_to_openai(result: Any, *, model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=result.text),
                finish_reason=FINISH_REASON_MAP.get(result.stop_reason, "stop"),
            )
        ],
        usage=Usage(
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
        ),
    )


def role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(role="assistant"))],
    )


def text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=text))],
    )


def final_chunk(stop_reason: str | None, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=DeltaMessage(),
                finish_reason=FINISH_REASON_MAP.get(stop_reason, "stop"),
            )
        ],
    )
