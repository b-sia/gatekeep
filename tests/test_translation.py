from dataclasses import dataclass

import pytest

from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    extract_text,
    final_chunk,
    openai_to_payload,
    resolve_route,
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


def test_resolve_route_alias_hit_routes_anthropic():
    assert resolve_route("gpt-4o", aliases=ALIASES) == ("anthropic", "claude-sonnet-5")


def test_resolve_route_claude_prefix_routes_anthropic():
    assert resolve_route("claude-opus-4-8", aliases=ALIASES) == (
        "anthropic",
        "claude-opus-4-8",
    )


def test_resolve_route_unrecognized_routes_ollama_unchanged():
    assert resolve_route("llama3.2", aliases=ALIASES) == ("ollama", "llama3.2")
    assert resolve_route("mystery", aliases=ALIASES) == ("ollama", "mystery")


def test_resolve_route_openai_prefix_routes_openai_and_strips_prefix():
    assert resolve_route("openai/gpt-4o", aliases=ALIASES) == ("openai", "gpt-4o")


def test_resolve_route_google_prefix_routes_google_and_strips_prefix():
    assert resolve_route("google/gemini-flash-latest", aliases=ALIASES) == (
        "google",
        "gemini-flash-latest",
    )


def test_resolve_route_prefix_bypasses_alias_table():
    # "gpt-4o" alone is aliased to Claude; the openai/ prefix must skip that.
    assert resolve_route("openai/gpt-4o", aliases=ALIASES) == ("openai", "gpt-4o")
    assert resolve_route("gpt-4o", aliases=ALIASES) == ("anthropic", "claude-sonnet-5")


def test_extract_text_flattens_multimodal_parts():
    assert extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"


def test_extract_text_passes_through_string():
    assert extract_text("hi") == "hi"


def test_extract_text_none_is_empty_string():
    assert extract_text(None) == ""


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
    provider, payload = openai_to_payload(
        req,
        default_max_tokens=4096,
        model_aliases=ALIASES,
    )
    assert provider == "anthropic"
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-sonnet-5"
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_default_max_tokens_applied():
    provider, payload = openai_to_payload(
        _req(),
        default_max_tokens=777,
        model_aliases=ALIASES,
    )
    assert payload["max_tokens"] == 777


def test_unrecognized_model_routes_ollama():
    provider, payload = openai_to_payload(
        _req(model="llama3.2"),
        default_max_tokens=4096,
        model_aliases=ALIASES,
    )
    assert provider == "ollama"
    assert payload["model"] == "llama3.2"


def test_no_conversational_message_raises():
    req = _req(messages=[{"role": "system", "content": "only system"}])
    with pytest.raises(TranslationError):
        openai_to_payload(
            req,
            default_max_tokens=10,
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
