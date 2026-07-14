from __future__ import annotations

import json
import logging
import time

import ollama
from redis.exceptions import RedisError
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from gatekeep.accounting import calculate_cost, log_request
from gatekeep.api.errors import map_provider_error, openai_error
from gatekeep.api.openai_schemas import ChatCompletionRequest, ChatMessage
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
from gatekeep.observability.metrics import (
    cache_cost_saved_usd,
    cache_exact_hits,
    cache_exact_misses,
    cache_semantic_hits,
    cache_semantic_misses,
    cache_semantic_similarity,
    observe_request,
    requests_total,
)
from gatekeep.prompts import PromptNotFoundError, get_prompt
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.base import StreamEnd, TextDelta
from gatekeep.providers.ollama import OllamaProvider
from gatekeep.routing import select_model
from gatekeep.samples import record_request_sample

logger = logging.getLogger(__name__)

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
    require_api_key and require_rate_limit raise), this returns it verbatim
    at the top level. Preserves any headers set on the exception (e.g.
    Retry-After from a 429), which would otherwise be silently dropped.
    """
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code, content=exc.detail, headers=exc.headers
        )
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


@app.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus-format metrics for scraping; unauthenticated like /healthz."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


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

    If `prompt_name` is set on the request, the active template registered
    under that name is resolved via `get_prompt` and prepended to
    `req.messages` as a system message, ahead of any system/developer
    messages the client also sent (openai_to_payload lifts and concatenates
    all of them into one `system` string, in message order).

    If `route_by_cost` is set on the request (and `prompt_name` is also set),
    the requested model may be substituted for a strictly cheaper one via
    `select_model`, but only when that cheaper model has a recent passing
    `EvalRun` at or above `quality_floor` (default 0.0) for the prompt's eval
    suite. Routing never activates unless explicitly opted into, and never
    substitutes a more expensive model. The substitution happens before the
    streaming/non-streaming branch, so a chosen model flows into both the
    provider call and (when `stream: true`) the SSE dispatch. What is scoped
    out of Phase 3 is `routed_from` accounting on the streaming path: `_sse`
    still calls `log_request` without `routed_from`, since routing decisions
    are informed by eval history that only the non-streaming path records
    samples for.
    """
    settings = get_settings()
    if req.prompt_name is not None:
        try:
            template = await get_prompt(req.prompt_name, session)
        except PromptNotFoundError as exc:
            return openai_error(400, str(exc), "invalid_request_error")
        req.messages = [ChatMessage(role="system", content=template)] + req.messages
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

    routed_from = None
    if req.route_by_cost and req.prompt_name is not None:
        floor = req.quality_floor if req.quality_floor is not None else 0.0
        chosen = await select_model(model, req.prompt_name, floor, session)
        if chosen != model:
            routed_from = model
            model = chosen
            payload["model"] = chosen

    requests_total.labels(model=model, key_id=str(key.id)).inc()

    if req.stream:
        return StreamingResponse(
            _sse(provider, payload, model, key_id=key.id),
            media_type="text/event-stream",
        )

    redis = get_redis(settings)
    request_hash = hash_request(payload)
    try:
        cached = await get_cached_response(redis, request_hash)
    except RedisError:
        logger.warning(
            "Exact cache lookup failed (Redis unavailable); treating as a cache miss."
        )
        cached = None
    if cached is not None:
        cache_exact_hits.labels(model=model).inc()
        cost_usd = calculate_cost(
            model, cached.usage.prompt_tokens, cached.usage.completion_tokens
        )
        cache_cost_saved_usd.inc(cost_usd)
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
        observe_request(
            model=model,
            key_id=key.id,
            prompt_tokens=cached.usage.prompt_tokens,
            completion_tokens=cached.usage.completion_tokens,
            cost_usd=cost_usd,
        )
        return JSONResponse(content=cached.model_dump())
    cache_exact_misses.labels(model=model).inc()

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
            cache_semantic_hits.labels(model=model).inc()
            cache_semantic_similarity.labels(model=model).observe(
                semantic_match.similarity
            )
            cache_cost_saved_usd.inc(semantic_match.cached.cost_usd)
            semantic_response = build_response_from_cache(semantic_match.cached)
            await log_request(
                session,
                key_id=key.id,
                model=model,
                prompt_tokens=semantic_response.usage.prompt_tokens,
                completion_tokens=semantic_response.usage.completion_tokens,
                response_id=semantic_response.id,
                cached=True,
                cache_key="semantic",
                cost_usd_override=semantic_match.cached.cost_usd,
            )
            observe_request(
                model=model,
                key_id=key.id,
                prompt_tokens=semantic_response.usage.prompt_tokens,
                completion_tokens=semantic_response.usage.completion_tokens,
                cost_usd=semantic_match.cached.cost_usd,
            )
            return JSONResponse(content=semantic_response.model_dump())
        cache_semantic_misses.labels(model=model).inc()

    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        return map_provider_error(exc)
    response = result_to_openai(result, model=model)
    try:
        await set_cached_response(
            redis,
            request_hash,
            response,
            ttl_seconds=settings.cache_exact_ttl_seconds,
            prompt_name=req.prompt_name,
        )
    except RedisError:
        logger.warning(
            "Exact cache write failed (Redis unavailable); serving response uncached."
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
            prompt_name=req.prompt_name,
        )
    if req.prompt_name is not None:
        await record_request_sample(
            session,
            key_id=key.id,
            prompt_name=req.prompt_name,
            model=model,
            input_messages=payload["messages"],
            output_text=response.choices[0].message.content or "",
        )
    await log_request(
        session,
        key_id=key.id,
        model=model,
        prompt_tokens=result.input_tokens,
        completion_tokens=result.output_tokens,
        response_id=response.id,
        prompt_name=req.prompt_name,
        routed_from=routed_from,
    )
    observe_request(
        model=model,
        key_id=key.id,
        prompt_tokens=result.input_tokens,
        completion_tokens=result.output_tokens,
        cost_usd=calculate_cost(model, result.input_tokens, result.output_tokens),
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
                observe_request(
                    model=model,
                    key_id=key_id,
                    prompt_tokens=ev.input_tokens,
                    completion_tokens=ev.output_tokens,
                    cost_usd=calculate_cost(model, ev.input_tokens, ev.output_tokens),
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
