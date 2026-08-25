import pytest
from pydantic import ValidationError

from gatekeep.api.anthropic_schemas import (
    ContentBlock,
    MessagesRequest,
    MessagesResponse,
    MessagesUsage,
)


def test_messages_request_requires_max_tokens():
    with pytest.raises(ValidationError):
        MessagesRequest.model_validate(
            {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
        )


def test_messages_request_requires_at_least_one_message():
    with pytest.raises(ValidationError):
        MessagesRequest.model_validate(
            {"model": "claude-sonnet-5", "messages": [], "max_tokens": 10}
        )


def test_messages_request_defaults():
    req = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
    )
    assert req.system is None
    assert req.stream is False
    assert req.prompt_name is None
    assert req.route_by_cost is False


def test_messages_response_round_trips():
    resp = MessagesResponse(
        id="msg_1",
        model="claude-sonnet-5",
        content=[ContentBlock(text="hi")],
        stop_reason="end_turn",
        usage=MessagesUsage(input_tokens=1, output_tokens=1),
    )
    dumped = resp.model_dump()
    assert dumped["type"] == "message"
    assert dumped["role"] == "assistant"
    assert dumped["content"] == [{"type": "text", "text": "hi"}]
