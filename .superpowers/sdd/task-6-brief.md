### Task 6: Anthropic provider wrapper

**Files:**
- Create: `gatekeep/providers/__init__.py`
- Create: `gatekeep/providers/anthropic.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- Consumes: the `anthropic` async SDK client (injected — not constructed here, so tests use a fake).
- Produces:
  - `@dataclass CompletionResult(text: str, input_tokens: int, output_tokens: int, stop_reason: str | None)`
  - `@dataclass TextDelta(text: str)`
  - `@dataclass StreamEnd(stop_reason: str | None, input_tokens: int, output_tokens: int)`
  - `class AnthropicProvider(client)` with `async def complete(payload: dict) -> CompletionResult` and `async def stream(payload: dict) -> AsyncIterator[TextDelta | StreamEnd]`.

- [ ] **Step 1: Write the failing test `tests/test_provider.py`** (fakes model the SDK's shapes)

```python
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
    assert result.stop_reason == "end_turn"


async def test_stream_yields_deltas_then_end():
    provider = AnthropicProvider(FakeClient(FakeMessagesStreaming()))
    events = [e async for e in provider.stream({"model": "claude-sonnet-5", "messages": [], "max_tokens": 10})]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "max_tokens"
    assert ends[0].output_tokens == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.providers'`.

- [ ] **Step 3: Create empty `gatekeep/providers/__init__.py`, then write `gatekeep/providers/anthropic.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


@dataclass
class TextDelta:
    text: str


@dataclass
class StreamEnd:
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class AnthropicProvider:
    """Thin async wrapper over the Anthropic SDK client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        message = await self._client.messages.create(**payload)
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return CompletionResult(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        async with self._client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                yield TextDelta(text=text)
            final = await stream.get_final_message()
            yield StreamEnd(
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provider.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/providers/__init__.py gatekeep/providers/anthropic.py tests/test_provider.py
git commit -m "feat: anthropic provider wrapper with normalized result types"
```

---

