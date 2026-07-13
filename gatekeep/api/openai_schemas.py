from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel


class ChatMessage(BaseModel):
    """A single message in an OpenAI-style chat completion request."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]], None] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """The OpenAI `/v1/chat/completions` request body."""

    model_config = {"extra": "allow"}

    model: str
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: bool = False
    stop: Union[str, list[str], None] = None
    prompt_name: Optional[str] = None


class Usage(BaseModel):
    """Token usage for a completion, in OpenAI's field naming."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    """The assistant message returned inside a non-streaming completion choice."""

    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None


class Choice(BaseModel):
    """One completion choice in a non-streaming chat completion response."""

    index: int = 0
    message: ResponseMessage
    finish_reason: Optional[str] = None


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

    role: Optional[str] = None
    content: Optional[str] = None


class ChunkChoice(BaseModel):
    """One choice within a streaming chat completion chunk."""

    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """One Server-Sent Events chunk of a streaming chat completion."""

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
