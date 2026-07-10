from __future__ import annotations

import json
import time

import ollama
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from gatekeep.accounting import calculate_cost, log_request
from gatekeep.api.errors import map_provider_error, openai_error
from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    final_chunk,
    new_completion_id,
    openai_to_payload,
    result_to_openai,
    role_chunk,
    text_chunk,
)
from gatekeep.config import get_settings
from gatekeep.db import SessionLocal, get_session
from gatekeep.embeddings import embed_text
from gatekeep.middleware.cache_exact import (
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.cache_semantic import (
    build_response_from_cache,
    extract_embeddable_text,
    find_semantic_match,
    store_cached_response,
)
from gatekeep.middleware.ratelimit import get_redis, require_rate_limit
from gatekeep.models import ApiKey
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.base import StreamEnd, TextDelta
from gatekeep.providers.ollama import OllamaProvider

app = FastAPI(title="gatekeep")

_settings = get_settings()
_providers: dict[str, AnthropicProvider | OllamaProvider] = {
    "anthropic": AnthropicProvider(AsyncAnthropic(api_key=_settings.anthropic_api_key)),
    "ollama": OllamaProvider(ollama.AsyncClient(host=_settings.ollama_host)),
}


def get_provider(name: str) -> AnthropicProvider | OllamaProvider:
    """Look up the pre-built provider instance for a resolved provider name."""
    return _providers[name]


@app.exception_handler(FastAPIHTTPException)
async def _http_exception_handler(
    request: Request, exc: FastAPIHTTPException
) -> JSONResponse:
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    key: ApiKey = Depends(require_rate_limit),
    session: AsyncSession = Depends(get_session),
):
    """OpenAI-compatible chat completions endpoint, routed per-request by model.

    Requires a valid, rate-limit-unexhausted API key. Translates the request
    to a provider-neutral payload, resolves which provider (Anthropic or
    Ollama) should serve the requested model, then either streams the
    response as SSE (when `stream: true`) or returns a single OpenAI-shaped
    JSON completion. Non-streaming requests are first checked against the
    exact-match cache; on a miss, the semantic cache is checked next (an
    embedding-similarity match above threshold); a miss on both falls
    through to the provider, and the fresh response is written to both
    caches afterwards. Every completed request is logged via `log_request`
    for cost accounting.
    """
    settings = get_settings()
    try:
        provider_name, payload = openai_to_payload(
            req,
            default_max_tokens=settings.default_max_tokens,
            model_aliases=settings.model_aliases,
        )
    except TranslationError as exc:
        return openai_error(400, str(exc), "invalid_request_error")

    provider = get_provider(provider_name)
    model = payload["model"]

    if req.stream:
        return StreamingResponse(
            _sse(provider, payload, model, key_id=key.id),
            media_type="text/event-stream",
        )

    redis = get_redis(settings)
    request_hash = hash_request(payload)
    cached = await get_cached_response(redis, request_hash)
    if cached is not None:
        await log_request(
            session,
            key_id=key.id,
            model=model,
            prompt_tokens=cached.usage.prompt_tokens,
            completion_tokens=cached.usage.completion_tokens,
            response_id=cached.id,
            cached=True,
            cache_key=request_hash,
        )
        return JSONResponse(content=cached.model_dump())

    embeddable_text = extract_embeddable_text(payload)
    embedding = embed_text(embeddable_text)
    if embedding is not None:
        semantic_match = await find_semantic_match(
            session,
            embedding,
            model=model,
            threshold=settings.semantic_cache_similarity_threshold,
            max_age_seconds=settings.cache_exact_ttl_seconds,
        )
        if semantic_match is not None:
            semantic_response = build_response_from_cache(semantic_match)
            await log_request(
                session,
                key_id=key.id,
                model=model,
                prompt_tokens=semantic_response.usage.prompt_tokens,
                completion_tokens=semantic_response.usage.completion_tokens,
                response_id=semantic_response.id,
                cached=True,
                cache_key="semantic",
                cost_usd_override=semantic_match.cost_usd,
            )
            return JSONResponse(content=semantic_response.model_dump())

    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error(exc)
    response = result_to_openai(result, model=model)
    await set_cached_response(
        redis, request_hash, response, ttl_seconds=settings.cache_exact_ttl_seconds
    )
    if embedding is not None:
        await store_cached_response(
            session,
            exact_hash=request_hash,
            user_messages_text=embeddable_text,
            embedding=embedding,
            response_text=response.choices[0].message.content or "",
            model=model,
            cost_usd=calculate_cost(model, result.input_tokens, result.output_tokens),
        )
    await log_request(
        session,
        key_id=key.id,
        model=model,
        prompt_tokens=result.input_tokens,
        completion_tokens=result.output_tokens,
        response_id=response.id,
    )
    return JSONResponse(content=response.model_dump())


async def _sse(
    provider: AnthropicProvider | OllamaProvider,
    payload: dict,
    model: str,
    *,
    key_id: int,
):
    """Stream a chat completion as OpenAI-style Server-Sent Events.

    Emits a role chunk, then a text chunk per delta, then a final chunk
    carrying the finish_reason. An upstream error mid-stream is surfaced
    as an in-band error event before the closing [DONE]. Logs the request
    via `log_request` once the stream ends, using its own DB session since
    this generator keeps running after the request-scoped session dependency
    has already been closed.
    """
    completion_id = new_completion_id()
    created = int(time.time())
    yield _event(role_chunk(id=completion_id, created=created, model=model))
    try:
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                yield _event(
                    text_chunk(ev.text, id=completion_id, created=created, model=model)
                )
            elif isinstance(ev, StreamEnd):
                yield _event(
                    final_chunk(
                        ev.stop_reason, id=completion_id, created=created, model=model
                    )
                )
                async with SessionLocal() as session:
                    await log_request(
                        session,
                        key_id=key_id,
                        model=model,
                        prompt_tokens=ev.input_tokens,
                        completion_tokens=ev.output_tokens,
                        response_id=completion_id,
                    )
    except Exception as exc:  # surface upstream errors inside the stream
        error_payload = {
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "code": "provider_error",
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
    yield "data: [DONE]\n\n"


def _event(chunk) -> str:
    """Format a ChatCompletionChunk as one SSE `data:` event."""
    return f"data: {chunk.model_dump_json()}\n\n"
