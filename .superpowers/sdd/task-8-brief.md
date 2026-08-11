### Task 8: FastAPI app + `/v1/chat/completions` endpoint

**Files:**
- Create: `gatekeep/app.py`
- Test: `tests/test_endpoint.py`

**Interfaces:**
- Consumes: everything above — `require_api_key`, `openai_to_anthropic`, `AnthropicProvider`, translation chunk helpers, `map_anthropic_error`.
- Produces: `gatekeep.app.app` (FastAPI), `gatekeep.app.get_provider()` dependency (overridable in tests), routes `GET /healthz` and `POST /v1/chat/completions`.

- [ ] **Step 1: Write `gatekeep/app.py`**

```python
from __future__ import annotations

import time

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from gatekeep.api.errors import map_anthropic_error, openai_error
from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    final_chunk,
    new_completion_id,
    openai_to_anthropic,
    result_to_openai,
    role_chunk,
    text_chunk,
)
from gatekeep.config import get_settings
from gatekeep.middleware.auth import require_api_key
from gatekeep.providers.anthropic import AnthropicProvider, StreamEnd, TextDelta

app = FastAPI(title="gatekeep")


def get_provider() -> AnthropicProvider:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return AnthropicProvider(client)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    _key=Depends(require_api_key),
    provider: AnthropicProvider = Depends(get_provider),
):
    settings = get_settings()
    try:
        payload = openai_to_anthropic(
            req,
            default_max_tokens=settings.default_max_tokens,
            default_model=settings.default_model,
            model_aliases=settings.model_aliases,
        )
    except TranslationError as exc:
        return openai_error(400, str(exc), "invalid_request_error")

    model = payload["model"]

    if req.stream:
        return StreamingResponse(
            _sse(provider, payload, model),
            media_type="text/event-stream",
        )

    try:
        result = await provider.complete(payload)
    except Exception as exc:  # anthropic.APIError and friends
        return map_anthropic_error(exc)
    return JSONResponse(content=result_to_openai(result, model=model).model_dump())


async def _sse(provider: AnthropicProvider, payload: dict, model: str):
    completion_id = new_completion_id()
    created = int(time.time())
    yield _event(role_chunk(id=completion_id, created=created, model=model))
    try:
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                yield _event(text_chunk(ev.text, id=completion_id, created=created, model=model))
            elif isinstance(ev, StreamEnd):
                yield _event(
                    final_chunk(ev.stop_reason, id=completion_id, created=created, model=model)
                )
    except Exception as exc:  # surface upstream errors inside the stream
        yield f'data: {{"error": {{"message": {_json(str(exc))}, "type": "upstream_error"}}}}\n\n'
    yield "data: [DONE]\n\n"


def _event(chunk) -> str:
    return f"data: {chunk.model_dump_json()}\n\n"


def _json(s: str) -> str:
    import json

    return json.dumps(s)
```

- [ ] **Step 2: Write the failing test `tests/test_endpoint.py`** (overrides `get_provider` with a fake; uses a real DB session via ASGI transport)

```python
import httpx
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.app import app, get_provider
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


class FakeProvider:
    async def complete(self, payload):
        assert "temperature" not in payload
        return CompletionResult(text="pong", input_tokens=3, output_tokens=1, stop_reason="end_turn")

    async def stream(self, payload):
        for t in ["po", "ng"]:
            yield TextDelta(text=t)
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=2)


@pytest_asyncio.fixture
async def raw_key(session):
    raw = generate_key()
    session.add(ApiKey(name="c", key_hash=hash_key(raw)))
    await session.commit()
    return raw


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_provider] = lambda: FakeProvider()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_requires_auth(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


async def test_non_streaming_completion(client, raw_key):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "temperature": 0.7,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["usage"]["total_tokens"] == 4


async def test_streaming_completion(client, raw_key):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        chunks = [line async for line in r.aiter_lines()]
    text = "".join(chunks)
    assert "chat.completion.chunk" in text
    assert '"content":"po"' in text
    assert "[DONE]" in text
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_endpoint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.app'` (or, once the file exists but before it's correct, an assertion failure). Postgres must be running.

- [ ] **Step 4: (App already written in Step 1) Run the full suite to verify everything passes**

Run: `pytest -v`
Expected: PASS — all tests across all tasks green. Postgres and Redis must be up (`docker compose up -d postgres redis`).

- [ ] **Step 5: Manual smoke test against real Claude (optional but recommended)**

Run:
```bash
# .env must contain a real ANTHROPIC_API_KEY
docker compose up -d postgres redis
alembic upgrade head
export $(grep -v '^#' .env | xargs)
KEY=$(python scripts/create_key.py "smoke test")
uvicorn gatekeep.app:app --port 8000 &
sleep 2
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Say hi in 3 words"}]}' | python -m json.tool
kill %1
```
Expected: a JSON `chat.completion` object whose `choices[0].message.content` is a short greeting, and `model` is `claude-sonnet-5`.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/app.py tests/test_endpoint.py
git commit -m "feat: fastapi app with /v1/chat/completions (streaming + non-streaming)"
```

---

## Phase 1 Definition of Done

- `pytest -v` is fully green with Postgres + Redis running.
- `docker compose up` brings up the gateway; `GET /healthz` returns `{"status":"ok"}`.
- A client using the OpenAI SDK with `base_url=http://localhost:8000/v1` and a `gk-` key gets real Claude completions, streaming and non-streaming.
- Sampling params are dropped; unknown models resolve to `claude-sonnet-5`; missing/invalid keys return 401 in OpenAI error shape.

## Self-Review Notes (traceability to spec)

- OpenAI-compatible `/v1/chat/completions`, stream + non-stream → Tasks 4, 5, 8.
- Translation layer incl. streaming + error mapping → Tasks 5, 7, 8.
- `providers/anthropic.py` async client with retries/streaming/usage → Task 6 (SDK provides retries/backoff by default; usage extracted in `complete`/`stream`).
- API-key auth (static keys in Postgres) → Tasks 3, 7.
- Postgres + Redis via docker-compose → Task 2 (Redis provisioned; consumed in Phase 2).
- **Deferred to later phases (correctly out of Phase 1 scope):** rate limiting, caching, cost accounting/logging, prompt registry, eval gate, curation, Prometheus/Grafana, GitHub Actions. These are Phases 2–4.
