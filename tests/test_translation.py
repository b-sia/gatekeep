from dataclasses import dataclass

import pytest

from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    final_chunk,
    openai_to_anthropic,
    resolve_model,
    result_to_openai,
    role_chunk,
    text_chunk,
)


@dataclass
class FakeResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


ALIASES = {"gpt-4o": "claude-sonnet-5"}


def _req(**kw):
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return ChatCompletionRequest.model_validate(base)


def test_resolve_model_alias_passthrough_default():
    assert (
        resolve_model("gpt-4o", default_model="claude-sonnet-5", aliases=ALIASES)
        == "claude-sonnet-5"
    )
    assert (
        resolve_model(
            "claude-opus-4-8", default_model="claude-sonnet-5", aliases=ALIASES
        )
        == "claude-opus-4-8"
    )
    assert (
        resolve_model("mystery", default_model="claude-sonnet-5", aliases=ALIASES)
        == "claude-sonnet-5"
    )


def test_system_message_lifted_and_sampling_dropped():
    req = _req(
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        temperature=0.9,
        top_p=0.5,
        max_tokens=100,
    )
    payload = openai_to_anthropic(
        req,
        default_max_tokens=4096,
        default_model="claude-sonnet-5",
        model_aliases=ALIASES,
    )
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-sonnet-5"
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_default_max_tokens_applied():
    payload = openai_to_anthropic(
        _req(),
        default_max_tokens=777,
        default_model="claude-sonnet-5",
        model_aliases=ALIASES,
    )
    assert payload["max_tokens"] == 777


def test_no_conversational_message_raises():
    req = _req(messages=[{"role": "system", "content": "only system"}])
    with pytest.raises(TranslationError):
        openai_to_anthropic(
            req,
            default_max_tokens=10,
            default_model="claude-sonnet-5",
            model_aliases=ALIASES,
        )


def test_result_to_openai_passes_through_canonical_finish_reason():
    result = FakeResult(
        text="hello", input_tokens=3, output_tokens=2, stop_reason="stop"
    )
    resp = result_to_openai(result, model="claude-sonnet-5")
    assert resp.choices[0].message.content == "hello"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.prompt_tokens == 3
    assert resp.usage.completion_tokens == 2
    assert resp.usage.total_tokens == 5
    assert resp.id.startswith("chatcmpl-")


def test_result_to_openai_defaults_missing_finish_reason_to_stop():
    result = FakeResult(text="hello", input_tokens=1, output_tokens=1, stop_reason=None)
    resp = result_to_openai(result, model="claude-sonnet-5")
    assert resp.choices[0].finish_reason == "stop"


def test_stream_chunk_helpers():
    rc = role_chunk(id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert rc.choices[0].delta.role == "assistant"
    tc = text_chunk("hi", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert tc.choices[0].delta.content == "hi"
    fc = final_chunk("length", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert fc.choices[0].finish_reason == "length"
    assert fc.choices[0].delta.content is None
