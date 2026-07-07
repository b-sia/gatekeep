from types import SimpleNamespace

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta
from gatekeep.providers.ollama import OllamaProvider


class FakeOllamaClient:
    def __init__(self, response=None, chunks=None):
        self._response = response
        self._chunks = chunks or []
        self.last_call = None

    async def chat(self, **kwargs):
        self.last_call = kwargs
        if kwargs.get("stream"):
            return self._achunks()
        return self._response

    async def _achunks(self):
        for chunk in self._chunks:
            yield chunk


def _response(
    content="hello world", done_reason="stop", prompt_eval_count=5, eval_count=2
):
    return SimpleNamespace(
        message=SimpleNamespace(content=content),
        done_reason=done_reason,
        prompt_eval_count=prompt_eval_count,
        eval_count=eval_count,
    )


async def test_complete_returns_normalized_result():
    client = FakeOllamaClient(response=_response())
    provider = OllamaProvider(client)
    result = await provider.complete(
        {"model": "llama3.2", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert isinstance(result, CompletionResult)
    assert result.text == "hello world"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.stop_reason == "stop"


async def test_complete_maps_length_stop_reason():
    client = FakeOllamaClient(response=_response(done_reason="length"))
    provider = OllamaProvider(client)
    result = await provider.complete(
        {"model": "llama3.2", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert result.stop_reason == "length"


async def test_complete_defaults_missing_usage_to_zero():
    response = SimpleNamespace(
        message=SimpleNamespace(content="hi"),
        done_reason="stop",
        prompt_eval_count=None,
        eval_count=None,
    )
    client = FakeOllamaClient(response=response)
    provider = OllamaProvider(client)
    result = await provider.complete(
        {"model": "llama3.2", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert result.input_tokens == 0
    assert result.output_tokens == 0


async def test_complete_folds_system_into_leading_message():
    client = FakeOllamaClient(response=_response())
    provider = OllamaProvider(client)
    await provider.complete(
        {
            "model": "llama3.2",
            "system": "be terse",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        }
    )
    assert client.last_call["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert client.last_call["options"]["num_predict"] == 50


async def test_stream_yields_deltas_then_end():
    chunks = [
        SimpleNamespace(message=SimpleNamespace(content="hel"), done=False),
        SimpleNamespace(message=SimpleNamespace(content="lo"), done=False),
        SimpleNamespace(
            message=SimpleNamespace(content=""),
            done=True,
            done_reason="stop",
            prompt_eval_count=4,
            eval_count=2,
        ),
    ]
    client = FakeOllamaClient(chunks=chunks)
    provider = OllamaProvider(client)
    events = [
        e
        async for e in provider.stream(
            {"model": "llama3.2", "messages": [{"role": "user", "content": "hi"}]}
        )
    ]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "stop"
    assert ends[0].input_tokens == 4
    assert ends[0].output_tokens == 2
