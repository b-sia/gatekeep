from types import SimpleNamespace

from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta
from gatekeep.providers.google import GoogleProvider


class FakeModels:
    def __init__(self, response=None, chunks=None):
        self._response = response
        self._chunks = chunks or []
        self.last_call = None

    async def generate_content(self, **kwargs):
        self.last_call = kwargs
        return self._response

    async def generate_content_stream(self, **kwargs):
        # Mirrors the real google-genai SDK: this is a coroutine that must be
        # awaited to get back an async iterator, not itself an async
        # generator - GoogleProvider.stream awaits it before `async for`-ing.
        self.last_call = kwargs
        chunks = self._chunks

        async def _iterator():
            for chunk in chunks:
                yield chunk

        return _iterator()


class FakeAio:
    def __init__(self, models):
        self.models = models


class FakeGoogleClient:
    def __init__(self, models):
        self.aio = FakeAio(models)


def _response(text="hello world", finish_reason="STOP", prompt=5, candidates=2):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=candidates
        ),
    )


async def test_complete_returns_normalized_result():
    client = FakeGoogleClient(FakeModels(response=_response()))
    provider = GoogleProvider(client)
    result = await provider.complete(
        {
            "model": "gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
    )
    assert isinstance(result, CompletionResult)
    assert result.text == "hello world"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.stop_reason == "stop"


async def test_complete_maps_max_tokens_stop_reason():
    client = FakeGoogleClient(FakeModels(response=_response(finish_reason="MAX_TOKENS")))
    provider = GoogleProvider(client)
    result = await provider.complete(
        {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    )
    assert result.stop_reason == "length"


async def test_complete_maps_prohibited_content_stop_reason():
    client = FakeGoogleClient(
        FakeModels(response=_response(finish_reason="PROHIBITED_CONTENT"))
    )
    provider = GoogleProvider(client)
    result = await provider.complete(
        {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    )
    assert result.stop_reason == "content_filter"


async def test_complete_maps_assistant_role_to_model():
    models = FakeModels(response=_response())
    client = FakeGoogleClient(models)
    provider = GoogleProvider(client)
    await provider.complete(
        {
            "model": "gemini-2.5-flash",
            "system": "be terse",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            "max_tokens": 50,
        }
    )
    assert models.last_call["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
    ]
    assert models.last_call["config"]["system_instruction"] == "be terse"
    assert models.last_call["config"]["max_output_tokens"] == 50


def _stream_chunk(text, prompt=4, candidates=2, finish_reason=None):
    # Mirrors the real API: every chunk carries cumulative usage_metadata,
    # but candidates[0].finish_reason is only populated on the terminal chunk.
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt, candidates_token_count=candidates
        ),
    )


async def test_stream_yields_deltas_then_end():
    chunks = [
        _stream_chunk("hel"),
        _stream_chunk("lo"),
        _stream_chunk(None, finish_reason="STOP"),
    ]
    client = FakeGoogleClient(FakeModels(chunks=chunks))
    provider = GoogleProvider(client)
    events = [
        e
        async for e in provider.stream(
            {"model": "gemini-flash-latest", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
        )
    ]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "stop"
    assert ends[0].input_tokens == 4
    assert ends[0].output_tokens == 2
