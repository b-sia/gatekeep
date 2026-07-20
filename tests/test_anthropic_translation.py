from dataclasses import dataclass

from gatekeep.api.anthropic_schemas import MessagesRequest
from gatekeep.api.anthropic_translation import (
    content_block_delta_event,
    content_block_start_event,
    content_block_stop_event,
    message_delta_event,
    message_start_event,
    message_stop_event,
    messages_to_payload,
    new_message_id,
    openai_response_to_messages,
    result_to_messages,
    reverse_finish_reason,
)
from gatekeep.api.openai_schemas import ChatCompletionResponse, Choice, ResponseMessage, Usage


@dataclass
class FakeResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


ALIASES = {"gpt-4o": "claude-sonnet-5"}


def _req(**kw):
    base = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }
    base.update(kw)
    return MessagesRequest.model_validate(base)


def test_messages_to_payload_basic():
    provider, payload = messages_to_payload(_req(), model_aliases=ALIASES)
    assert provider == "anthropic"
    assert payload == {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }


def test_messages_to_payload_carries_system_and_stop_sequences():
    provider, payload = messages_to_payload(
        _req(system="be terse", stop_sequences=["STOP"]), model_aliases=ALIASES
    )
    assert payload["system"] == "be terse"
    assert payload["stop_sequences"] == ["STOP"]


def test_messages_to_payload_resolves_via_shared_alias_table():
    provider, payload = messages_to_payload(_req(model="gpt-4o"), model_aliases=ALIASES)
    assert provider == "anthropic"
    assert payload["model"] == "claude-sonnet-5"


def test_messages_to_payload_flattens_multimodal_content():
    provider, payload = messages_to_payload(
        _req(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]),
        model_aliases=ALIASES,
    )
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_reverse_finish_reason_maps_canonical_to_anthropic_vocabulary():
    assert reverse_finish_reason("stop") == "end_turn"
    assert reverse_finish_reason("length") == "max_tokens"
    assert reverse_finish_reason("tool_calls") == "tool_use"
    assert reverse_finish_reason("content_filter") == "refusal"
    assert reverse_finish_reason(None) == "end_turn"


def test_result_to_messages_shapes_response():
    result = FakeResult(text="hi there", input_tokens=3, output_tokens=2, stop_reason="stop")
    resp = result_to_messages(result, model="claude-sonnet-5")
    assert resp.content[0].text == "hi there"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 3
    assert resp.usage.output_tokens == 2
    assert resp.model == "claude-sonnet-5"


def test_openai_response_to_messages_converts_cached_shape():
    cached = ChatCompletionResponse(
        id="chatcmpl-1",
        created=0,
        model="claude-sonnet-5",
        choices=[
            Choice(message=ResponseMessage(content="cached text"), finish_reason="length")
        ],
        usage=Usage(prompt_tokens=4, completion_tokens=6, total_tokens=10),
    )
    resp = openai_response_to_messages(cached)
    assert resp.content[0].text == "cached text"
    assert resp.stop_reason == "max_tokens"
    assert resp.usage.input_tokens == 4
    assert resp.usage.output_tokens == 6
    assert resp.model == "claude-sonnet-5"


def test_new_message_id_has_msg_prefix():
    assert new_message_id().startswith("msg_")


def test_message_start_event_shape():
    ev = message_start_event(id="msg_1", model="claude-sonnet-5")
    assert ev["type"] == "message_start"
    assert ev["message"]["id"] == "msg_1"
    assert ev["message"]["model"] == "claude-sonnet-5"
    assert ev["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_content_block_delta_event_carries_text():
    ev = content_block_delta_event("hi")
    assert ev == {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "text_delta", "text": "hi"},
    }


def test_message_delta_event_carries_final_usage():
    ev = message_delta_event(stop_reason="end_turn", input_tokens=3, output_tokens=2)
    assert ev["delta"]["stop_reason"] == "end_turn"
    assert ev["usage"] == {"input_tokens": 3, "output_tokens": 2}


def test_content_block_start_and_stop_and_message_stop_shapes():
    assert content_block_start_event()["type"] == "content_block_start"
    assert content_block_stop_event() == {"type": "content_block_stop", "index": 0}
    assert message_stop_event() == {"type": "message_stop"}
