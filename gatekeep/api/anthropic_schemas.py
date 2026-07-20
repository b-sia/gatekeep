from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


class MessageParam(BaseModel):
    """A single message in an Anthropic-style Messages API request."""

    role: Literal["user", "assistant"]
    content: Union[str, list[dict[str, Any]]]


class MessagesRequest(BaseModel):
    """The Anthropic `/v1/messages` request body, plus gatekeep's own extensions.

    `prompt_name`, `route_by_cost`, and `quality_floor` mirror the OpenAI-compat
    endpoint's extensions (see `ChatCompletionRequest`), letting the same
    prompt-registry and cost-routing features work from either API surface.
    """

    model_config = {"extra": "allow"}

    model: str
    messages: list[MessageParam] = Field(min_length=1)
    system: Optional[Union[str, list[dict[str, Any]]]] = None
    max_tokens: int
    stop_sequences: Optional[list[str]] = None
    stream: bool = False
    prompt_name: Optional[str] = None
    route_by_cost: bool = False
    quality_floor: Optional[float] = None


class ContentBlock(BaseModel):
    """One block of a Messages API response's `content` list (text-only in v1)."""

    type: Literal["text"] = "text"
    text: str


class MessagesUsage(BaseModel):
    """Token usage in Anthropic's field naming."""

    input_tokens: int
    output_tokens: int


class MessagesResponse(BaseModel):
    """The Anthropic-shaped response body for a non-streaming `/v1/messages` call."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str
    content: list[ContentBlock]
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: MessagesUsage
