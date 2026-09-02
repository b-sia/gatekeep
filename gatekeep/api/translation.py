from __future__ import annotations

import time
import uuid
from typing import Any, Literal

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


class TranslationError(ValueError):
    """Raised when an OpenAI request cannot be mapped to Anthropic."""


def extract_text(content: Any) -> str:
    """Flatten an OpenAI message's content (string or multimodal parts) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def resolve_route(
    requested: str, *, aliases: dict[str, str]
) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], str]:
    """Determine which provider should serve `requested` and its resolved model id.

    A `openai/`, `google/`, or `stub/` prefix routes directly to that
    provider with the prefix stripped (e.g. "openai/gpt-4o" ->
    ("openai", "gpt-4o")), bypassing the alias table entirely - this is how a
    client opts into the real upstream (or the load-test stub) instead of the
    alias table's Claude substitution. Otherwise checks the alias table,
    passes through anything already prefixed `claude-` as Anthropic, and
    otherwise routes to Ollama with the model name unchanged (assumed to be a
    local Ollama tag).
    """
    if requested.startswith("openai/"):
        return "openai", requested.removeprefix("openai/")
    if requested.startswith("google/"):
        return "google", requested.removeprefix("google/")
    if requested.startswith("stub/"):
        return "stub", requested.removeprefix("stub/")
    if requested in aliases:
        return "anthropic", aliases[requested]
    if requested.startswith("claude-"):
        return "anthropic", requested
    return "ollama", requested


def openai_to_payload(
    req: ChatCompletionRequest,
    *,
    default_max_tokens: int,
    model_aliases: dict[str, str],
) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], dict[str, Any]]:
    """Build a provider-neutral completion payload from an OpenAI chat completion request.

    Lifts `system`/`developer` messages into a single `system` string,
    resolves the target provider and model via `resolve_route`, and always
    sets `max_tokens`. Sampling parameters (`temperature`/`top_p`/`top_k`)
    are never forwarded, since Anthropic rejects non-default values for them.

    Raises TranslationError if a message role is unsupported or no
    user/assistant message remains after lifting system content out.
    """
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for msg in req.messages:
        text = extract_text(msg.content)
        if msg.role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": text})
        else:  # "tool" and anything else
            raise TranslationError(f"unsupported message role in v1: {msg.role}")

    if not messages:
        raise TranslationError("request must contain at least one user or assistant message")

    provider, model = resolve_route(req.model, aliases=model_aliases)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens or req.max_completion_tokens or default_max_tokens,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if req.stop:
        payload["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    # temperature/top_p/top_k intentionally omitted (rejected by Sonnet 5 / Opus 4.8).
    return provider, payload


def new_completion_id() -> str:
    """Generate a fresh OpenAI-style `chatcmpl-...` completion ID."""
    return "chatcmpl-" + uuid.uuid4().hex


def result_to_openai(result: Any, *, model: str) -> ChatCompletionResponse:
    """Convert a normalized provider CompletionResult into an OpenAI chat completion response."""
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=result.text),
                finish_reason=result.stop_reason or "stop",
            )
        ],
        usage=Usage(
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
        ),
    )


def role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk:
    """Build the first SSE chunk of a stream, announcing the assistant role."""
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(role="assistant"))],
    )


def text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    """Build a mid-stream SSE chunk carrying one text delta."""
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=text))],
    )


def final_chunk(
    stop_reason: str | None, *, id: str, created: int, model: str
) -> ChatCompletionChunk:
    """Build the terminal SSE chunk carrying the already-canonical OpenAI finish_reason."""
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=DeltaMessage(),
                finish_reason=stop_reason or "stop",
            )
        ],
    )
