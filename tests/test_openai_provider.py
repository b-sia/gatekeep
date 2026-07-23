from types import SimpleNamespace

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta
from gatekeep.providers.openai import OpenAIProvider


class FakeCompletions:
    def __init__(self, response=None, chunks=None):
        self._response = response
        self._chunks = chunks or []
        self.last_call = None

    async def create(self, **kwargs):
        self.last_call = kwargs
        if kwargs.get("stream"):
            return self._achunks()
        return self._response

    async def _achunks(self):
        for chunk in self._chunks:
            yield chunk


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeOpenAIClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def _response(content="hello world", finish_reason="stop", prompt=5, completion=2):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


async def test_complete_returns_normalized_result():
    client = FakeOpenAIClient(FakeCompletions(response=_response()))
    provider = OpenAIProvider(client)
    result = await provider.complete(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    )
    assert isinstance(result, CompletionResult)
    assert result.text == "hello world"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.stop_reason == "stop"


async def test_complete_maps_length_stop_reason():
    client = FakeOpenAIClient(
        FakeCompletions(response=_response(finish_reason="length"))
    )
    provider = OpenAIProvider(client)
    result = await provider.complete(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    )
    assert result.stop_reason == "length"


async def test_complete_folds_system_into_leading_message():
    completions = FakeCompletions(response=_response())
    client = FakeOpenAIClient(completions)
    provider = OpenAIProvider(client)
    await provider.complete(
        {
            "model": "gpt-4o",
            "system": "be terse",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
        }
    )
    assert completions.last_call["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    assert completions.last_call["max_tokens"] == 50


async def test_complete_passes_stop_sequences_as_stop():
    completions = FakeCompletions(response=_response())
    client = FakeOpenAIClient(completions)
    provider = OpenAIProvider(client)
    await provider.complete(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 50,
            "stop_sequences": ["STOP"],
        }
    )
    assert completions.last_call["stop"] == ["STOP"]


async def test_stream_yields_deltas_then_end():
    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="hel"), finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="lo"), finish_reason="stop")],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2),
        ),
    ]
    client = FakeOpenAIClient(FakeCompletions(chunks=chunks))
    provider = OpenAIProvider(client)
    events = [
        e
        async for e in provider.stream(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
        )
    ]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "stop"
    assert ends[0].input_tokens == 4
    assert ends[0].output_tokens == 2
