### Task 5: Translation layer (pure, SDK-free)

**Files:**
- Create: `gatekeep/api/translation.py`
- Test: `tests/test_translation.py`

**Interfaces:**
- Consumes: `gatekeep.api.openai_schemas` models; `gatekeep.providers.anthropic.CompletionResult` (defined in Task 6 — see the shape below and keep names identical).
- Produces:
  - `resolve_model(requested: str, *, default_model: str, aliases: dict[str, str]) -> str`
  - `openai_to_anthropic(req: ChatCompletionRequest, *, default_max_tokens: int, default_model: str, model_aliases: dict[str, str]) -> dict[str, Any]` — returns a kwargs dict for `messages.create`/`.stream` containing `model`, `messages`, `max_tokens`, optionally `system` and `stop_sequences`. Never contains `temperature`/`top_p`/`top_k`.
  - `result_to_openai(result: CompletionResult, *, model: str) -> ChatCompletionResponse`
  - `role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk` (initial assistant-role delta)
  - `text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk`
  - `final_chunk(stop_reason: str | None, *, id: str, created: int, model: str) -> ChatCompletionChunk`
  - `FINISH_REASON_MAP: dict[str, str]`
  - `class TranslationError(ValueError)`

The `CompletionResult` shape this task assumes (Task 6 defines it): a dataclass with `text: str`, `input_tokens: int`, `output_tokens: int`, `stop_reason: str | None`.

- [ ] **Step 1: Write the failing test `tests/test_translation.py`**

```python
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
    assert resolve_model("gpt-4o", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-sonnet-5"
    assert resolve_model("claude-opus-4-8", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-opus-4-8"
    assert resolve_model("mystery", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-sonnet-5"


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
        req, default_max_tokens=4096, default_model="claude-sonnet-5", model_aliases=ALIASES
    )
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-sonnet-5"
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_default_max_tokens_applied():
    payload = openai_to_anthropic(
        _req(), default_max_tokens=777, default_model="claude-sonnet-5", model_aliases=ALIASES
    )
    assert payload["max_tokens"] == 777


def test_no_conversational_message_raises():
    req = _req(messages=[{"role": "system", "content": "only system"}])
    with pytest.raises(TranslationError):
        openai_to_anthropic(
            req, default_max_tokens=10, default_model="claude-sonnet-5", model_aliases=ALIASES
        )


def test_result_to_openai_maps_usage_and_finish_reason():
    result = FakeResult(text="hello", input_tokens=3, output_tokens=2, stop_reason="end_turn")
    resp = result_to_openai(result, model="claude-sonnet-5")
    assert resp.choices[0].message.content == "hello"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.prompt_tokens == 3
    assert resp.usage.completion_tokens == 2
    assert resp.usage.total_tokens == 5
    assert resp.id.startswith("chatcmpl-")


def test_stream_chunk_helpers():
    rc = role_chunk(id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert rc.choices[0].delta.role == "assistant"
    tc = text_chunk("hi", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert tc.choices[0].delta.content == "hi"
    fc = final_chunk("max_tokens", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert fc.choices[0].finish_reason == "length"
    assert fc.choices[0].delta.content is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_translation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.api.translation'`.

- [ ] **Step 3: Write `gatekeep/api/translation.py`**

```python
from __future__ import annotations

import time
import uuid
from typing import Any

from gatekeep.api.openai_schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    DeltaMessage,
    ResponseMessage,
    Usage,
)

FINISH_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class TranslationError(ValueError):
    """Raised when an OpenAI request cannot be mapped to Anthropic."""


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def resolve_model(requested: str, *, default_model: str, aliases: dict[str, str]) -> str:
    if requested in aliases:
        return aliases[requested]
    if requested.startswith("claude-"):
        return requested
    return default_model


def openai_to_anthropic(
    req: ChatCompletionRequest,
    *,
    default_max_tokens: int,
    default_model: str,
    model_aliases: dict[str, str],
) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for msg in req.messages:
        text = _extract_text(msg.content)
        if msg.role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": text})
        else:  # "tool" and anything else
            raise TranslationError(f"unsupported message role in v1: {msg.role}")

    if not messages:
        raise TranslationError("request must contain at least one user or assistant message")

    payload: dict[str, Any] = {
        "model": resolve_model(req.model, default_model=default_model, aliases=model_aliases),
        "messages": messages,
        "max_tokens": req.max_tokens or req.max_completion_tokens or default_max_tokens,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if req.stop:
        payload["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    # temperature/top_p/top_k intentionally omitted (rejected by Sonnet 5 / Opus 4.8).
    return payload


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def result_to_openai(result: Any, *, model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=result.text),
                finish_reason=FINISH_REASON_MAP.get(result.stop_reason, "stop"),
            )
        ],
        usage=Usage(
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
        ),
    )


def role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(role="assistant"))],
    )


def text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=text))],
    )


def final_chunk(stop_reason: str | None, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=DeltaMessage(),
                finish_reason=FINISH_REASON_MAP.get(stop_reason, "stop"),
            )
        ],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_translation.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/translation.py tests/test_translation.py
git commit -m "feat: pure openai<->anthropic translation layer"
```

---

