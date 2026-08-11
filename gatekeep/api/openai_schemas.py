from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """A single message in an OpenAI-style chat completion request."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """The OpenAI `/v1/chat/completions` request body."""

    model_config = {"extra": "allow"}

    model: str
    messages: list[ChatMessage]
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    stream: bool = False
    stop: str | list[str] | None = None
    prompt_name: str | None = None
    route_by_cost: bool = False
    quality_floor: float | None = None


class Usage(BaseModel):
    """Token usage for a completion, in OpenAI's field naming."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    """The assistant message returned inside a non-streaming completion choice."""

    role: Literal["assistant"] = "assistant"
    content: str | None = None


class Choice(BaseModel):
    """One completion choice in a non-streaming chat completion response."""

    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    """The OpenAI-shaped response body for a non-streaming chat completion."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class DeltaMessage(BaseModel):
    """An incremental assistant-message delta within a streaming chunk."""

    role: str | None = None
    content: str | None = None


class ChunkChoice(BaseModel):
    """One choice within a streaming chat completion chunk."""

    index: int = 0
    delta: DeltaMessage
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """One Server-Sent Events chunk of a streaming chat completion."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
