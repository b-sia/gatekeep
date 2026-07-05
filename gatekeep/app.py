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
