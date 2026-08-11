### Task 4: OpenAI request/response schemas

**Files:**
- Create: `gatekeep/api/__init__.py`
- Create: `gatekeep/api/openai_schemas.py`
- Test: `tests/test_openai_schemas.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all pydantic `BaseModel`):
  - `ChatMessage(role: str, content: str | list[dict] | None, name: str | None)`
  - `ChatCompletionRequest(model: str, messages: list[ChatMessage], max_tokens: int | None, max_completion_tokens: int | None, temperature: float | None, top_p: float | None, stream: bool = False, stop: str | list[str] | None)`
  - `Usage(prompt_tokens, completion_tokens, total_tokens)`
  - `ResponseMessage(role="assistant", content: str | None)`
  - `Choice(index, message: ResponseMessage, finish_reason: str | None)`
  - `ChatCompletionResponse(id, object="chat.completion", created, model, choices, usage)`
  - `DeltaMessage(role: str | None, content: str | None)`
  - `ChunkChoice(index, delta: DeltaMessage, finish_reason: str | None)`
  - `ChatCompletionChunk(id, object="chat.completion.chunk", created, model, choices)`

- [ ] **Step 1: Write the failing test `tests/test_openai_schemas.py`**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openai_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.api'`.

- [ ] **Step 3: Create empty `gatekeep/api/__init__.py`, then write `gatekeep/api/openai_schemas.py`**

```python
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]], None] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: bool = False
    stop: Union[str, list[str], None] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openai_schemas.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/__init__.py gatekeep/api/openai_schemas.py tests/test_openai_schemas.py
git commit -m "feat: openai-compatible request/response schemas"
```

---

