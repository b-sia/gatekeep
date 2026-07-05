from gatekeep.api.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)


def test_parses_minimal_request():
    req = ChatCompletionRequest.model_validate(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert req.model == "gpt-4o"
    assert req.stream is False
    assert req.messages[0].content == "hi"


def test_response_serializes_openai_shape():
    resp = ChatCompletionResponse(
        id="chatcmpl-x",
        created=1,
        model="claude-sonnet-5",
        choices=[Choice(message=ResponseMessage(content="hello"), finish_reason="stop")],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )
    data = resp.model_dump()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] == 4
