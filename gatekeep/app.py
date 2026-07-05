from __future__ import annotations

import json
import time

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request

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


@app.exception_handler(FastAPIHTTPException)
async def _http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
    """Serialize HTTPException bodies as flat, top-level OpenAI-shaped errors.

    FastAPI's default handler nests HTTPException.detail under a "detail"
    key; when detail is already an OpenAI-shaped {"error": {...}} dict (as
    require_api_key raises), this returns it verbatim at the top level.
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return openai_error(exc.status_code, str(exc.detail), "invalid_request_error")


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return pydantic request-validation failures as an OpenAI-shaped 400."""
    return openai_error(400, str(exc), "invalid_request_error")


def get_provider() -> AnthropicProvider:
    """FastAPI dependency constructing an AnthropicProvider from settings."""
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return AnthropicProvider(client)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    _key=Depends(require_api_key),
    provider: AnthropicProvider = Depends(get_provider),
):
    """OpenAI-compatible chat completions endpoint, proxying to Claude.

    Requires a valid API key. Translates the request to Anthropic's Messages
    API, then either streams the response as SSE (when `stream: true`) or
    returns a single OpenAI-shaped JSON completion.
    """
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
    """Stream a chat completion as OpenAI-style Server-Sent Events.

    Emits a role chunk, then a text chunk per delta, then a final chunk
    carrying the mapped finish_reason. An upstream error mid-stream is
    surfaced as an in-band error event before the closing [DONE].
    """
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
        error_payload = {
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "code": "anthropic_error",
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
    yield "data: [DONE]\n\n"


def _event(chunk) -> str:
    """Format a ChatCompletionChunk as one SSE `data:` event."""
    return f"data: {chunk.model_dump_json()}\n\n"
