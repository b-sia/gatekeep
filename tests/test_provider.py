from types import SimpleNamespace

from gatekeep.providers.anthropic import (
    AnthropicProvider,
    CompletionResult,
    StreamEnd,
    TextDelta,
)


class FakeMessages:
    async def create(self, **payload):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello world")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            stop_reason="end_turn",
        )

    def stream(self, **payload):
        return FakeStream()


class FakeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    async def text_stream(self):  # not used; iteration below via __aiter__ shim
        raise NotImplementedError

    def __aiter__(self):
        raise NotImplementedError


class FakeStreamCtx:
    """Async context manager whose .text_stream yields deltas."""

    def __init__(self):
        self._deltas = ["hel", "lo"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=4, output_tokens=2),
            stop_reason="max_tokens",
        )


class FakeMessagesStreaming(FakeMessages):
    def stream(self, **payload):
        return FakeStreamCtx()


class FakeClient:
    def __init__(self, messages):
        self.messages = messages


async def test_complete_returns_normalized_result():
    provider = AnthropicProvider(FakeClient(FakeMessages()))
    result = await provider.complete({"model": "claude-sonnet-5", "messages": [], "max_tokens": 10})
    assert isinstance(result, CompletionResult)
    assert result.text == "hello world"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.stop_reason == "stop"


async def test_stream_yields_deltas_then_end():
    provider = AnthropicProvider(FakeClient(FakeMessagesStreaming()))
    events = [
        e
        async for e in provider.stream(
            {"model": "claude-sonnet-5", "messages": [], "max_tokens": 10}
        )
    ]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "length"
    assert ends[0].output_tokens == 2


async def test_complete_maps_unknown_stop_reason_to_stop():
    class FakeMessagesUnknownReason(FakeMessages):
        async def create(self, **payload):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="hi")],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                stop_reason="tool_use",
            )

    provider = AnthropicProvider(FakeClient(FakeMessagesUnknownReason()))
    result = await provider.complete({"model": "claude-sonnet-5", "messages": [], "max_tokens": 10})
    assert result.stop_reason == "tool_calls"
