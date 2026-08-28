from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import Any

import ollama
from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from openai import AsyncOpenAI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from gatekeep.accounts.accounting import (
    calculate_cost,
    enforce_pricing_policy,
    estimate_tokens,
    log_request,
)
from gatekeep.api.anthropic_schemas import MessagesRequest
from gatekeep.api.anthropic_translation import (
    content_block_delta_event,
    content_block_start_event,
    content_block_stop_event,
    message_delta_event,
    message_start_event,
    message_stop_event,
    messages_to_payload,
    new_message_id,
    openai_response_to_messages,
    result_to_messages,
    reverse_finish_reason,
)
from gatekeep.api.auth import auth_router
from gatekeep.api.dashboard import router as dashboard_router
from gatekeep.api.errors import (
    anthropic_error,
    map_provider_error,
    map_provider_error_anthropic,
    openai_error,
    openai_error_to_anthropic,
)
from gatekeep.api.openai_schemas import ChatCompletionRequest, ChatMessage
from gatekeep.api.translation import (
    TranslationError,
    extract_text,
    final_chunk,
    new_completion_id,
    openai_to_payload,
    result_to_openai,
    role_chunk,
    text_chunk,
)
from gatekeep.caching.embeddings import embed_text_async
from gatekeep.caching.embeddings import warm as warm_embedding_model
from gatekeep.config import get_settings
from gatekeep.middleware.budget import require_budget, run_budget_reconciliation_loop
from gatekeep.middleware.cache_exact import (
    get_cached_response,
    hash_request,
    set_cached_response,
)
from gatekeep.middleware.cache_semantic import (
    build_response_from_cache,
    extract_embeddable_text,
    find_semantic_match,
    run_cache_purge_loop,
    store_cached_response,
)
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.observability.latency import (
    LatencyMiddleware,
    StreamTimer,
    mark,
    observe_non_streaming,
)
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
from gatekeep.prompts.prompts import PromptNotFoundError, resolve_prompt_version_for_request
from gatekeep.prompts.samples import record_request_sample
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.base import StreamEnd, TextDelta
from gatekeep.providers.google import GoogleProvider
from gatekeep.providers.ollama import OllamaProvider
from gatekeep.providers.openai import OpenAIProvider
from gatekeep.routing.pricing import get_pricing_table
from gatekeep.routing.routing import select_model
from gatekeep.storage.db import SessionLocal, get_session
from gatekeep.storage.models import ApiKey

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Attach a handler to the "gatekeep" logger namespace, once.

    Only INFO-level logs under this namespace (e.g. the semantic-cache purge
    loop's "Purged N expired rows" line) get a handler here, not the root
    logger, so third-party libraries (httpx, huggingface_hub,
    sentence_transformers) don't gain INFO-level noise as a side effect.

    Called from `_lifespan` rather than at import time, so merely importing
    this module (e.g. a test grabbing `app` for a TestClient) has no global
    logging side effects. Guarded by the handler-count check so starting the
    app more than once in the same process - or any other double-invocation -
    can't stack duplicate handlers and duplicate every log line.
    """
    gatekeep_logger = logging.getLogger("gatekeep")
    if gatekeep_logger.handlers:
        return
    gatekeep_logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    gatekeep_logger.addHandler(handler)


# The four values the `path` label/column can take, matching the Prometheus
# `path` label one-for-one (observability/metrics.py) and RequestLog.path
# (models.py). Every call site below sources its value from one of these
# rather than a bare string literal, so the metric and the DB column cannot
# drift apart from a typo in either.
#
# `_STREAM_PATH` in particular is unlike the other three: the non-streaming
# branches route one `_finish_request` parameter into both `mark()` and
# `log_request()`, but the streaming path publishes its label from the
# endpoint and writes its column from inside the SSE generator - two
# different functions, and the generator holds `state` rather than
# `request`. Using the shared constant on both sides is what keeps those
# sites from drifting apart.
_CACHE_EXACT_PATH = "cache_exact"
_CACHE_SEMANTIC_PATH = "cache_semantic"
_PROVIDER_PATH = "provider"
_STREAM_PATH = "stream"


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Warm the embedding model and pricing table before serving traffic.

    `embed_text`'s underlying model loads lazily on first use, and on a
    container with no baked-in weights that first load downloads them from
    the HF Hub - tens of seconds of blocking work. Doing it here means the
    process doesn't report ready until it's paid, instead of stalling
    whichever request happens to arrive first (and every other request
    queued behind it on the event loop).

    `get_pricing_table()` is also loaded eagerly for the same reason, but for
    correctness rather than latency: it verifies the vendored pricing file
    against its committed hash pin (see gatekeep.routing.pricing), and that file is
    the spend-enforcement table. A corrupted file or a stale pin must fail
    the container at startup - loud and before it takes traffic - rather than
    surface as a generic 500 on whichever request happens to be first to call
    enforce_pricing_policy/calculate_cost.

    Also starts the budget-reconciliation background task (see
    `gatekeep.middleware.budget.run_budget_reconciliation_loop`), which
    periodically overwrites every account's Redis spend counter with a
    fresh DB aggregate so a dropped `record_spend` increment or float
    drift doesn't under-enforce budgets for the rest of the billing
    period (issue #27), and the semantic-cache purge background task (see
    `gatekeep.middleware.cache_semantic.run_cache_purge_loop`), which
    periodically deletes cached_responses rows past their TTL so the table
    (and the per-request cosine-distance scan cost against it) doesn't grow
    unboundedly (issue #26). Both are cancelled on shutdown along with
    everything else.
    """
    configure_logging()
    await asyncio.to_thread(warm_embedding_model)
    get_pricing_table()
    settings = get_settings()
    reconciliation_task = asyncio.create_task(
        run_budget_reconciliation_loop(
            SessionLocal,
            get_redis(),
            interval_seconds=settings.budget_reconcile_interval_seconds,
        )
    )
    cache_purge_task = asyncio.create_task(
        run_cache_purge_loop(
            SessionLocal,
            ttl_seconds=settings.cache_exact_ttl_seconds,
            interval_seconds=settings.cache_purge_interval_seconds,
        )
    )
    try:
        yield
    finally:
        reconciliation_task.cancel()
        cache_purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await reconciliation_task
        with suppress(asyncio.CancelledError):
            await cache_purge_task


app = FastAPI(title="gatekeep", lifespan=_lifespan)
# Added first so it wraps everything: the start stamp must land before any
# FastAPI dependency (auth, rate limit, budget) runs.
app.add_middleware(LatencyMiddleware)
app.include_router(dashboard_router)
app.include_router(auth_router)

_DASHBOARD_DIST = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "dist"

if (_DASHBOARD_DIST / "assets").is_dir():
    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=str(_DASHBOARD_DIST / "assets")),
        name="dashboard-assets",
    )


@app.get("/dashboard")
@app.get("/dashboard/{path:path}")
async def serve_dashboard(path: str = "") -> FileResponse:
    """Serve the dashboard SPA's index.html for any non-API path under
    `/dashboard`, so client-side routing/asset requests resolve correctly.

    Registered after `dashboard_router` (which owns `/dashboard/api/*`), so
    FastAPI matches the more specific API routes first. `dashboard_router`
    only defines literal paths, so a request under `/dashboard/api/...`
    that doesn't match any of them (a typo, a removed endpoint) would
    otherwise fall through to this catch-all and get served the SPA's HTML
    with a misleading 200 - `path` is checked for that case and rejected
    with a 404 instead.

    Args:
        path: The sub-path requested under `/dashboard` (e.g. `prompts` for
            `/dashboard/prompts`). Unused for SPA routes - every non-API
            path resolves to the same entry point, and client-side routing
            takes over from there.

    Returns:
        A `FileResponse` streaming the built `dashboard/dist/index.html`.

    Raises:
        HTTPException: 404 if `path` is an unmatched `/dashboard/api/*`
            request; 503 if the dashboard hasn't been built (`dashboard/dist`
            is missing), which would otherwise be an unhandled crash.
    """
    if path == "api" or path.startswith("api/"):
        raise FastAPIHTTPException(status_code=404, detail="Not Found")
    index_path = _DASHBOARD_DIST / "index.html"
    if not index_path.is_file():
        raise FastAPIHTTPException(
            status_code=503,
            detail="Dashboard is not built. Run `npm run build` in dashboard/.",
        )
    return FileResponse(index_path)


_settings = get_settings()
_GatewayProvider = AnthropicProvider | OllamaProvider | OpenAIProvider | GoogleProvider

_providers: dict[str, _GatewayProvider] = {
    "anthropic": AnthropicProvider(AsyncAnthropic(api_key=_settings.anthropic_api_key)),
    "ollama": OllamaProvider(ollama.AsyncClient(host=_settings.ollama_host)),
    # api_key falls back to a placeholder string (never None) so the SDK
    # client doesn't raise at import time when the key is unset - failures
    # surface as an upstream error on the first actual request instead, via
    # map_provider_error. See Settings.openai_api_key/google_api_key.
    "openai": OpenAIProvider(AsyncOpenAI(api_key=_settings.openai_api_key or "unset")),
    "google": GoogleProvider(genai.Client(api_key=_settings.google_api_key or "unset")),
}


def get_provider(name: str) -> _GatewayProvider:
    """Look up the pre-built provider instance for a resolved provider name."""
    return _providers[name]


async def _finish_request(
    request: Request,
    session: AsyncSession,
    *,
    model: str,
    provider: str,
    path: str,
    provider_ms: float | None,
    key_id: int,
    account_id: int,
    prompt_tokens: int,
    completion_tokens: int,
    response_id: str,
    cost_usd: float,
    cached: bool = False,
    cache_key: str | None = None,
    cost_usd_override: float | None = None,
    prompt_name: str | None = None,
    prompt_version_num: int | None = None,
    routed_from: str | None = None,
):
    """Publish latency labels and record accounting for one completed
    non-streaming request, at whichever branch (cache_exact, cache_semantic,
    or provider) actually served it.

    Bundles the four calls that otherwise have to be repeated at every branch
    exit: `mark` (publishes the histogram labels the middleware itself cannot
    resolve), `observe_non_streaming` (gateway overhead / provider duration
    histograms), `log_request` (the DB accounting row), and `observe_request`
    (token-count / cost histograms).

    Args:
        request: The Starlette request carrying `state.started_at`.
        session: DB session to persist the `RequestLog` row through.
        model: Resolved model id, used as the metric label.
        provider: Resolved upstream (`resolve_route`'s provider), passed
            through to `log_request` for its pricing lookup.
        path: One of "cache_exact", "cache_semantic", "provider". Published
            as the metric label and stored on the RequestLog row from this
            one parameter, so the histogram and the column cannot diverge.
        provider_ms: Upstream call duration, or None on a cache hit.
        key_id: The requesting API key's id.
        prompt_tokens: Input token count to record.
        completion_tokens: Output token count to record.
        response_id: The id to store on the `RequestLog` row.
        cost_usd: Cost to record on the token/cost histograms.
        cached: Whether this was served from a cache.
        cache_key: The cache key served from, if `cached`.
        cost_usd_override: Cost to store on `RequestLog` in place of a
            freshly calculated one, e.g. a semantic-cache hit logging the
            original generation's cost instead of $0.
        prompt_name: The prompt template served, if any.
        prompt_version_num: Which `PromptVersion` served the request, if any.
        routed_from: The originally requested model, if cost-routing chose a
            cheaper substitute.

    Returns:
        The `LatencyTimings` for this request.
    """
    mark(request, path=path)
    timings = observe_non_streaming(request, model=model, path=path, provider_ms=provider_ms)
    await log_request(
        session,
        key_id=key_id,
        account_id=account_id,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        response_id=response_id,
        cached=cached,
        cache_key=cache_key,
        cost_usd_override=cost_usd_override,
        prompt_name=prompt_name,
        prompt_version_num=prompt_version_num,
        routed_from=routed_from,
        path=path,
        duration_ms=timings.duration_ms,
        provider_ms=timings.provider_ms,
        ttft_ms=timings.ttft_ms,
    )
    observe_request(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
    )
    return timings


async def _finish_failed_request(
    request: Request,
    session: AsyncSession,
    *,
    model: str,
    provider: str,
    provider_started: float,
    key_id: int,
    account_id: int,
    response_id: str,
    prompt_name: str | None,
    routed_from: str | None,
    prompt_version_num: int | None,
) -> None:
    """Publish latency labels and record accounting for a non-streaming
    request whose provider call raised.

    Mirrors `_finish_request`'s bundling of `observe_non_streaming` +
    `log_request` for the success path, but for the `provider.complete(...)`
    exception branch of `chat_completions`/`messages`: zero tokens and zero
    cost (no partial output exists on this path, unlike the streaming
    generators which can estimate from accumulated delta text), and
    `outcome="provider_error"` unconditionally. Deliberately does not call
    `observe_request` (the token/cost histograms): a 0-token/`$0` observation
    would drag those histograms down with no informative signal, unlike the
    streaming failure paths, which have real (estimated) tokens/cost to
    contribute. Passes `count_latency=False` to `observe_non_streaming` for the
    same reason the DB latency percentiles exclude failed rows: `provider_ms`
    is still published so the middleware attributes gateway overhead, but a
    failed call's duration must not skew the provider-latency histogram.

    Args:
        request: The Starlette request carrying `state.started_at`.
        session: DB session to persist the `RequestLog` row through.
        model: Resolved model id, used as the metric label.
        provider: Resolved upstream (`resolve_route`'s provider), passed
            through to `log_request` for its pricing lookup.
        provider_started: `time.perf_counter()` value captured just before
            the provider call, used to compute `provider_ms`.
        key_id: The requesting API key's id.
        response_id: A freshly generated id - no real response exists on
            this path.
        prompt_name: The prompt template requested, if any.
        routed_from: The originally requested model, if cost-routing chose
            a cheaper substitute.
        prompt_version_num: Which `PromptVersion` was resolved, if any.
    """
    provider_ms = (time.perf_counter() - provider_started) * 1000
    timings = observe_non_streaming(
        request,
        model=model,
        path=_PROVIDER_PATH,
        provider_ms=provider_ms,
        count_latency=False,
    )
    # Best-effort accounting: this runs inside the caller's `except` branch,
    # right before it returns the mapped provider error. If the DB write itself
    # fails (connection reset, pool exhausted - often the very outage that made
    # the provider call fail), that failure must not propagate and mask the
    # real provider error as an uncaught 500. Log and swallow so the caller
    # still returns the mapped error to the client; a dropped accounting row is
    # the lesser harm.
    try:
        await log_request(
            session,
            key_id=key_id,
            account_id=account_id,
            provider=provider,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            response_id=response_id,
            prompt_name=prompt_name,
            routed_from=routed_from,
            prompt_version_num=prompt_version_num,
            path=_PROVIDER_PATH,
            outcome="provider_error",
            duration_ms=timings.duration_ms,
            provider_ms=timings.provider_ms,
            ttft_ms=timings.ttft_ms,
        )
    except Exception:
        logger.exception(
            "failed to record accounting for provider_error on the non-streaming "
            "path; returning the mapped provider error anyway"
        )


@app.exception_handler(FastAPIHTTPException)
async def _http_exception_handler(request: Request, exc: FastAPIHTTPException) -> JSONResponse:
    """Serialize HTTPException bodies as flat, top-level errors, OpenAI- or
    Anthropic-shaped depending on which endpoint raised them.

    FastAPI's default handler nests HTTPException.detail under a "detail"
    key; when detail is already an OpenAI-shaped {"error": {...}} dict (as
    require_api_key, require_rate_limit, and require_budget raise), this returns it verbatim
    at the top level for every endpoint except /v1/messages, whose real
    Anthropic SDK clients expect the {"type": "error", "error": {...}}
    envelope instead - so that shape is reconstructed via
    `openai_error_to_anthropic` for that path. Preserves any headers set on
    the exception (e.g. Retry-After from a 429), which would otherwise be
    silently dropped.
    """
    is_messages = request.url.path == "/v1/messages"
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = openai_error_to_anthropic(exc.detail) if is_messages else exc.detail
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)
    if is_messages:
        return anthropic_error(exc.status_code, str(exc.detail), "invalid_request_error")
    return openai_error(exc.status_code, str(exc.detail), "invalid_request_error")


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return pydantic request-validation failures as a 400, Anthropic-shaped
    for /v1/messages and OpenAI-shaped for every other endpoint."""
    if request.url.path == "/v1/messages":
        return anthropic_error(400, str(exc), "invalid_request_error")
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
    request: Request,
    req: ChatCompletionRequest,
    key: ApiKey = Depends(require_budget),
    session: AsyncSession = Depends(get_session),
):
    """OpenAI-compatible chat completions endpoint, routed per-request by model.

    Requires a valid, rate-limit-unexhausted, budget-unexceeded API key.
    Translates the request to a provider-neutral payload, resolves which
    provider (Anthropic or Ollama) should serve the requested model, then
    either streams the response as SSE (when `stream: true`) or returns a
    single OpenAI-shaped JSON completion. Non-streaming requests are first
    checked against the exact-match cache; on a miss, the semantic cache is
    checked next (an embedding-similarity match above threshold); a miss on
    both falls through to the provider, and the fresh response is written to
    both caches afterwards. Every completed request is logged via
    `log_request` for cost accounting.

    If `prompt_name` is set on the request, the template served is resolved
    via `resolve_prompt_version_for_request`, which returns the active
    version unless the prompt has an A/B candidate configured, in which case
    it randomly routes some percentage of requests to the candidate version
    instead (see prompts.py for the split logic). Whichever version is
    resolved, its template is prepended to `req.messages` as a system
    message, ahead of any system/developer messages the client also sent
    (openai_to_payload lifts and concatenates all of them into one `system`
    string, in message order). The resolved version's number is recorded on
    the resulting `RequestLog` row as `prompt_version_num`, so cost/eval
    metrics can later be compared active-vs-candidate.

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
    # Read the id once, up front, rather than touching `key.id` later on. A
    # concurrent duplicate insert makes store_cached_response roll back
    # (cache_semantic.py), and a rollback expires every object in the session
    # even under expire_on_commit=False - after which `key.id` would attempt an
    # implicit lazy refresh that async SQLAlchemy forbids, raising
    # MissingGreenlet and turning a benign cache race into a 500.
    key_id = key.id
    account_id = key.account_id
    served_prompt_version: int | None = None
    if req.prompt_name is not None:
        try:
            version = await resolve_prompt_version_for_request(req.prompt_name, session)
        except PromptNotFoundError as exc:
            return openai_error(400, str(exc), "invalid_request_error")
        served_prompt_version = version.version_num
        req.messages = [ChatMessage(role="system", content=version.template)] + req.messages
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
        chosen = await select_model(provider_name, model, req.prompt_name, floor, session)
        if chosen != model:
            routed_from = model
            model = chosen
            payload["model"] = chosen

    rejection = enforce_pricing_policy(provider_name, model)
    if rejection is not None:
        return openai_error(400, rejection, "invalid_request_error")

    requests_total.labels(model=model).inc()
    mark(request, model=model)

    if req.stream:
        mark(request, path=_STREAM_PATH)
        return StreamingResponse(
            _sse(
                provider,
                provider_name,
                payload,
                model,
                key_id=key_id,
                account_id=account_id,
                prompt_name=req.prompt_name,
                routed_from=routed_from,
                prompt_version_num=served_prompt_version,
                state=request.scope["state"],
            ),
            media_type="text/event-stream",
        )

    redis = get_redis(settings)
    request_hash = hash_request(payload)
    try:
        cached = await get_cached_response(redis, account_id, request_hash)
    except RedisError:
        logger.warning("Exact cache lookup failed (Redis unavailable); treating as a cache miss.")
        cached = None
    if cached is not None:
        cache_exact_hits.labels(model=model).inc()
        cost_usd = calculate_cost(
            provider_name, model, cached.usage.prompt_tokens, cached.usage.completion_tokens
        )
        cache_cost_saved_usd.inc(cost_usd)
        await _finish_request(
            request,
            session,
            model=model,
            provider=provider_name,
            path=_CACHE_EXACT_PATH,
            provider_ms=None,
            key_id=key_id,
            account_id=account_id,
            prompt_tokens=cached.usage.prompt_tokens,
            completion_tokens=cached.usage.completion_tokens,
            response_id=cached.id,
            cost_usd=cost_usd,
            cached=True,
            cache_key=request_hash,
            prompt_name=req.prompt_name,
            prompt_version_num=served_prompt_version,
        )
        return JSONResponse(content=cached.model_dump())
    cache_exact_misses.labels(model=model).inc()

    embeddable_text = extract_embeddable_text(payload)
    embedding = await embed_text_async(embeddable_text)
    if embedding is not None:
        semantic_match = await find_semantic_match(
            session,
            embedding,
            account_id=account_id,
            model=model,
            threshold=settings.semantic_cache_similarity_threshold,
            max_age_seconds=settings.cache_exact_ttl_seconds,
            max_tokens=payload["max_tokens"],
            stop_sequences=payload.get("stop_sequences"),
            prompt_version_num=served_prompt_version,
        )
        if semantic_match is not None:
            cache_semantic_hits.labels(model=model).inc()
            cache_semantic_similarity.labels(model=model).observe(semantic_match.similarity)
            cache_cost_saved_usd.inc(semantic_match.cached.cost_usd)
            semantic_response = build_response_from_cache(semantic_match.cached)
            await _finish_request(
                request,
                session,
                model=model,
                provider=provider_name,
                path=_CACHE_SEMANTIC_PATH,
                provider_ms=None,
                key_id=key_id,
                account_id=account_id,
                prompt_tokens=semantic_response.usage.prompt_tokens,
                completion_tokens=semantic_response.usage.completion_tokens,
                response_id=semantic_response.id,
                cost_usd=semantic_match.cached.cost_usd,
                cached=True,
                cache_key="semantic",
                cost_usd_override=semantic_match.cached.cost_usd,
                prompt_name=req.prompt_name,
                prompt_version_num=served_prompt_version,
            )
            return JSONResponse(content=semantic_response.model_dump())
        cache_semantic_misses.labels(model=model).inc()

    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        await _finish_failed_request(
            request,
            session,
            model=model,
            provider=provider_name,
            provider_started=provider_started,
            key_id=key_id,
            account_id=account_id,
            response_id=new_completion_id(),
            prompt_name=req.prompt_name,
            routed_from=routed_from,
            prompt_version_num=served_prompt_version,
        )
        return map_provider_error(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
    response = result_to_openai(result, model=model)
    try:
        await set_cached_response(
            redis,
            account_id,
            request_hash,
            response,
            ttl_seconds=settings.cache_exact_ttl_seconds,
            prompt_name=req.prompt_name,
        )
    except RedisError:
        logger.warning("Exact cache write failed (Redis unavailable); serving response uncached.")
    if embedding is not None:
        await store_cached_response(
            session,
            account_id=account_id,
            exact_hash=request_hash,
            user_messages_text=embeddable_text,
            embedding=embedding,
            response_text=response.choices[0].message.content or "",
            model=model,
            cost_usd=calculate_cost(
                provider_name, model, result.input_tokens, result.output_tokens
            ),
            max_tokens=payload["max_tokens"],
            stop_sequences=payload.get("stop_sequences"),
            prompt_name=req.prompt_name,
            prompt_version_num=served_prompt_version,
        )
    if req.prompt_name is not None:
        await record_request_sample(
            session,
            key_id=key_id,
            account_id=account_id,
            prompt_name=req.prompt_name,
            model=model,
            input_messages=payload["messages"],
            output_text=response.choices[0].message.content or "",
        )
    await _finish_request(
        request,
        session,
        model=model,
        provider=provider_name,
        path=_PROVIDER_PATH,
        provider_ms=provider_ms,
        key_id=key_id,
        account_id=account_id,
        prompt_tokens=result.input_tokens,
        completion_tokens=result.output_tokens,
        response_id=response.id,
        cost_usd=calculate_cost(provider_name, model, result.input_tokens, result.output_tokens),
        prompt_name=req.prompt_name,
        routed_from=routed_from,
        prompt_version_num=served_prompt_version,
    )
    return JSONResponse(content=response.model_dump())


@app.post("/v1/messages")
async def messages(
    request: Request,
    req: MessagesRequest,
    key: ApiKey = Depends(require_budget),
    session: AsyncSession = Depends(get_session),
):
    """Anthropic-native /v1/messages endpoint, sharing every middleware with
    /v1/chat/completions (auth, rate limiting, budget cap, tiered cache, cost-aware
    routing, accounting) but speaking the real Messages API request/response
    shape instead of OpenAI's. See `messages_to_payload` for why this needs
    far less translation than the OpenAI-compat path: gatekeep's internal
    payload is already Anthropic-shaped.

    `prompt_name` and `route_by_cost` behave identically to their
    `/v1/chat/completions` counterparts, including A/B candidate resolution
    via `resolve_prompt_version_for_request` and `prompt_version_num`
    accounting. The exact/semantic cache is shared across both endpoints
    (same request-hash derivation over the same provider-neutral payload);
    a hit is converted from the cache's stored OpenAI shape via
    `openai_response_to_messages`.
    """
    settings = get_settings()
    # Read the id once, up front, rather than touching `key.id` later on. A
    # concurrent duplicate insert makes store_cached_response roll back
    # (cache_semantic.py), and a rollback expires every object in the session
    # even under expire_on_commit=False - after which `key.id` would attempt an
    # implicit lazy refresh that async SQLAlchemy forbids, raising
    # MissingGreenlet and turning a benign cache race into a 500.
    key_id = key.id
    account_id = key.account_id
    served_prompt_version: int | None = None
    if req.prompt_name is not None:
        try:
            version = await resolve_prompt_version_for_request(req.prompt_name, session)
        except PromptNotFoundError as exc:
            return anthropic_error(400, str(exc), "invalid_request_error")
        served_prompt_version = version.version_num
        existing_system = extract_text(req.system) if req.system is not None else ""
        req.system = (
            f"{version.template}\n\n{existing_system}" if existing_system else version.template
        )

    provider_name, payload = messages_to_payload(req, model_aliases=settings.model_aliases)
    provider = get_provider(provider_name)
    model = payload["model"]

    routed_from = None
    if req.route_by_cost and req.prompt_name is not None:
        floor = req.quality_floor if req.quality_floor is not None else 0.0
        chosen = await select_model(provider_name, model, req.prompt_name, floor, session)
        if chosen != model:
            routed_from = model
            model = chosen
            payload["model"] = chosen

    rejection = enforce_pricing_policy(provider_name, model)
    if rejection is not None:
        return anthropic_error(400, rejection, "invalid_request_error")

    requests_total.labels(model=model).inc()
    mark(request, model=model)

    if req.stream:
        mark(request, path=_STREAM_PATH)
        return StreamingResponse(
            _messages_sse(
                provider,
                provider_name,
                payload,
                model,
                key_id=key_id,
                account_id=account_id,
                prompt_name=req.prompt_name,
                routed_from=routed_from,
                prompt_version_num=served_prompt_version,
                state=request.scope["state"],
            ),
            media_type="text/event-stream",
        )

    redis = get_redis(settings)
    request_hash = hash_request(payload)
    try:
        cached = await get_cached_response(redis, account_id, request_hash)
    except RedisError:
        logger.warning("Exact cache lookup failed (Redis unavailable); treating as a cache miss.")
        cached = None
    if cached is not None:
        cache_exact_hits.labels(model=model).inc()
        cost_usd = calculate_cost(
            provider_name, model, cached.usage.prompt_tokens, cached.usage.completion_tokens
        )
        cache_cost_saved_usd.inc(cost_usd)
        await _finish_request(
            request,
            session,
            model=model,
            provider=provider_name,
            path=_CACHE_EXACT_PATH,
            provider_ms=None,
            key_id=key_id,
            account_id=account_id,
            prompt_tokens=cached.usage.prompt_tokens,
            completion_tokens=cached.usage.completion_tokens,
            response_id=cached.id,
            cost_usd=cost_usd,
            cached=True,
            cache_key=request_hash,
            prompt_name=req.prompt_name,
            prompt_version_num=served_prompt_version,
        )
        return JSONResponse(content=openai_response_to_messages(cached).model_dump())
    cache_exact_misses.labels(model=model).inc()

    embeddable_text = extract_embeddable_text(payload)
    embedding = await embed_text_async(embeddable_text)
    if embedding is not None:
        semantic_match = await find_semantic_match(
            session,
            embedding,
            account_id=account_id,
            model=model,
            threshold=settings.semantic_cache_similarity_threshold,
            max_age_seconds=settings.cache_exact_ttl_seconds,
            max_tokens=payload["max_tokens"],
            stop_sequences=payload.get("stop_sequences"),
            prompt_version_num=served_prompt_version,
        )
        if semantic_match is not None:
            cache_semantic_hits.labels(model=model).inc()
            cache_semantic_similarity.labels(model=model).observe(semantic_match.similarity)
            cache_cost_saved_usd.inc(semantic_match.cached.cost_usd)
            semantic_response = build_response_from_cache(semantic_match.cached)
            await _finish_request(
                request,
                session,
                model=model,
                provider=provider_name,
                path=_CACHE_SEMANTIC_PATH,
                provider_ms=None,
                key_id=key_id,
                account_id=account_id,
                prompt_tokens=semantic_response.usage.prompt_tokens,
                completion_tokens=semantic_response.usage.completion_tokens,
                response_id=semantic_response.id,
                cost_usd=semantic_match.cached.cost_usd,
                cached=True,
                cache_key="semantic",
                cost_usd_override=semantic_match.cached.cost_usd,
                prompt_name=req.prompt_name,
                prompt_version_num=served_prompt_version,
            )
            return JSONResponse(content=openai_response_to_messages(semantic_response).model_dump())
        cache_semantic_misses.labels(model=model).inc()

    # Marked before the call so a provider error still carries labels.
    mark(request, path=_PROVIDER_PATH)
    provider_started = time.perf_counter()
    try:
        result = await provider.complete(payload)
    except Exception as exc:  # provider SDK error, e.g. anthropic.APIError
        await _finish_failed_request(
            request,
            session,
            model=model,
            provider=provider_name,
            provider_started=provider_started,
            key_id=key_id,
            account_id=account_id,
            response_id=new_message_id(),
            prompt_name=req.prompt_name,
            routed_from=routed_from,
            prompt_version_num=served_prompt_version,
        )
        return map_provider_error_anthropic(exc)
    provider_ms = (time.perf_counter() - provider_started) * 1000
    openai_shaped = result_to_openai(result, model=model)
    messages_response = result_to_messages(result, model=model)
    try:
        await set_cached_response(
            redis,
            account_id,
            request_hash,
            openai_shaped,
            ttl_seconds=settings.cache_exact_ttl_seconds,
            prompt_name=req.prompt_name,
        )
    except RedisError:
        logger.warning("Exact cache write failed (Redis unavailable); serving response uncached.")
    if embedding is not None:
        await store_cached_response(
            session,
            account_id=account_id,
            exact_hash=request_hash,
            user_messages_text=embeddable_text,
            embedding=embedding,
            response_text=messages_response.content[0].text,
            model=model,
            cost_usd=calculate_cost(
                provider_name, model, result.input_tokens, result.output_tokens
            ),
            max_tokens=payload["max_tokens"],
            stop_sequences=payload.get("stop_sequences"),
            prompt_name=req.prompt_name,
            prompt_version_num=served_prompt_version,
        )
    if req.prompt_name is not None:
        await record_request_sample(
            session,
            key_id=key_id,
            account_id=account_id,
            prompt_name=req.prompt_name,
            model=model,
            input_messages=payload["messages"],
            output_text=messages_response.content[0].text,
        )
    await _finish_request(
        request,
        session,
        model=model,
        provider=provider_name,
        path=_PROVIDER_PATH,
        provider_ms=provider_ms,
        key_id=key_id,
        account_id=account_id,
        prompt_tokens=result.input_tokens,
        completion_tokens=result.output_tokens,
        response_id=messages_response.id,
        cost_usd=calculate_cost(provider_name, model, result.input_tokens, result.output_tokens),
        prompt_name=req.prompt_name,
        routed_from=routed_from,
        prompt_version_num=served_prompt_version,
    )
    return JSONResponse(content=messages_response.model_dump())


async def _messages_sse(
    provider: _GatewayProvider,
    provider_name: str,
    payload: dict,
    model: str,
    *,
    key_id: int,
    account_id: int,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    state: dict | None = None,
):
    """Stream a /v1/messages completion as Anthropic-style named Server-Sent Events.

    `provider_name` is the resolved upstream (`resolve_route`'s provider),
    passed through to `log_request`/`calculate_cost` for pricing.

    Emits message_start, content_block_start, a content_block_delta per text
    delta, content_block_stop, message_delta (carrying the authoritative
    final usage and stop_reason on a clean finish), then message_stop.
    Accounting runs in a `finally` block so it fires on every exit path -
    see `_sse`'s docstring for the full outcome-tagging rationale (`ok` /
    `provider_error` / `client_disconnect`), which applies identically here,
    including its fourth case: a stream whose iteration ends without ever
    reaching `StreamEnd` raises `RuntimeError` and so is tagged
    `provider_error` with estimated tokens, rather than logged as a $0 `ok`
    row it has no authoritative counts to justify.

    Uses its own DB session for the same reason `_sse` does (the
    request-scoped session dependency is already closed by the time this
    generator keeps running), and wraps the accounting write in
    `_run_shielded` for the same cancellation-safety reason.

    `state` is `request.scope["state"]`, passed in because the generator runs
    after the endpoint has returned and can no longer reach the `request`
    object itself. It doubles as the channel back to the middleware:
    `StreamTimer.finish()` writes `provider_ms` onto it so the middleware can
    derive `gateway_overhead_seconds` once the stream closes, on every exit
    path including a failed one. The middleware still records end-to-end for
    this request; what the generator adds is TTFT, inter-token gaps, and
    time-to-last-token.
    """
    message_id = new_message_id()
    timer = StreamTimer(state, model=model)

    outcome = "ok"
    input_tokens = output_tokens = 0
    accumulated: list[str] = []
    stream_ended = False
    try:
        yield _anthropic_event("message_start", message_start_event(id=message_id, model=model))
        yield _anthropic_event("content_block_start", content_block_start_event())
        timer.provider_started()
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                timer.delta()
                accumulated.append(ev.text)
                yield _anthropic_event("content_block_delta", content_block_delta_event(ev.text))
            elif isinstance(ev, StreamEnd):
                stream_ended = True
                outcome = "ok"
                input_tokens, output_tokens = ev.input_tokens, ev.output_tokens
                yield _anthropic_event("content_block_stop", content_block_stop_event())
                yield _anthropic_event(
                    "message_delta",
                    message_delta_event(
                        stop_reason=reverse_finish_reason(ev.stop_reason),
                        input_tokens=ev.input_tokens,
                        output_tokens=ev.output_tokens,
                    ),
                )
                break
        if not stream_ended:
            # The provider finished a complete stream without emitting a
            # StreamEnd - the openai/google/ollama providers all gate it
            # conditionally, so their generators legitimately end without it.
            # The client received the full completion, so this is a success;
            # we only lack authoritative token counts and fall back to
            # estimates, then synthesize the terminal events the provider
            # never sent so the client still sees a well-formed stream.
            outcome = "ok"
            input_tokens = estimate_tokens(_payload_text(payload))
            output_tokens = estimate_tokens("".join(accumulated))
            yield _anthropic_event("content_block_stop", content_block_stop_event())
            yield _anthropic_event(
                "message_delta",
                message_delta_event(
                    stop_reason=reverse_finish_reason(None),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
            )
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_disconnect"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        raise
    except Exception as exc:  # surface upstream errors inside the stream
        outcome = "provider_error"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        yield _anthropic_event(
            "error",
            {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
        )
    finally:
        # NEVER yield here - illegal during GeneratorExit.
        timings = timer.finish(succeeded=(outcome == "ok"))
        cost_usd = calculate_cost(provider_name, model, input_tokens, output_tokens)

        async def _record() -> None:
            async with SessionLocal() as session:
                await log_request(
                    session,
                    key_id=key_id,
                    account_id=account_id,
                    provider=provider_name,
                    model=model,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    response_id=message_id,
                    prompt_name=prompt_name,
                    routed_from=routed_from,
                    prompt_version_num=prompt_version_num,
                    path=_STREAM_PATH,
                    outcome=outcome,
                    duration_ms=timings.duration_ms,
                    provider_ms=timings.provider_ms,
                    ttft_ms=timings.ttft_ms,
                )
            observe_request(
                model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                cost_usd=cost_usd,
            )

        await _run_shielded(_record())
    # Unreachable on the client-disconnect path, same as _sse.
    yield _anthropic_event("message_stop", message_stop_event())


def _anthropic_event(event_type: str, data: dict) -> str:
    """Format one named Anthropic-style SSE event (`event: <type>` line + `data: <json>` line)."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _run_shielded(coro: Coroutine[Any, Any, Any]) -> Any:
    """Await `coro` to completion, even if the calling task is cancelled one
    or more times while doing so.

    Used for the streaming generators' failure-path accounting: their
    `finally` block runs during `GeneratorExit`/`asyncio.CancelledError`
    (a client disconnecting mid-stream), and a real disconnect can inject
    cancellation more than once - e.g. a persistent cancel scope keeps
    re-raising it at every `await` until the scope itself exits. A bare
    `await coro` there would let a second cancellation cut the DB commit
    short and drop the row. `asyncio.shield` protects the wrapped task from
    the *outer* cancellation, but the outer `await` still raises
    CancelledError immediately when cancelled - so this loops, re-awaiting
    the same underlying task, until that task has actually finished.

    Scoped to callers in a `finally` block that either already have an
    exception in flight (which resumes propagating once this returns) or are
    about to end the generator; it is not a general-purpose "run to
    completion no matter what" utility, since it silently discards the
    caller's OWN cancellation once the wrapped coroutine completes.

    Args:
        coro: The coroutine to run to completion.

    Returns:
        Whatever `coro` returns.

    Raises:
        asyncio.CancelledError: only once the wrapped task itself is done
            (either with a result or, in principle, its own cancellation -
            which nothing here ever triggers).
        BaseException: whatever `coro` itself raises.
    """
    task = asyncio.ensure_future(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                raise


def _payload_text(payload: dict) -> str:
    """Concatenate a provider-neutral payload's system and message text.

    Used to estimate input tokens on a failed/aborted stream, where no
    authoritative provider-reported count exists (see `estimate_tokens`).
    `payload["messages"]` entries are always `{"role": ..., "content": str}`
    by the time they reach here (openai_to_payload/messages_to_payload have
    already flattened multimodal content to plain text).

    Args:
        payload: The provider-neutral payload built by openai_to_payload or
            messages_to_payload.

    Returns:
        Every message's text (and the system text, if present), joined by
        blank lines.
    """
    parts: list[str] = []
    if "system" in payload:
        parts.append(payload["system"])
    parts.extend(msg["content"] for msg in payload["messages"])
    return "\n\n".join(parts)


async def _sse(
    provider: _GatewayProvider,
    provider_name: str,
    payload: dict,
    model: str,
    *,
    key_id: int,
    account_id: int,
    prompt_name: str | None = None,
    routed_from: str | None = None,
    prompt_version_num: int | None = None,
    state: dict | None = None,
):
    """Stream a chat completion as OpenAI-style Server-Sent Events.

    `provider_name` is the resolved upstream (`resolve_route`'s provider),
    passed through to `log_request`/`calculate_cost` for pricing.

    Emits a role chunk, then a text chunk per delta, then a final chunk
    carrying the finish_reason. Accounting (`StreamTimer.finish`,
    `log_request`, `observe_request`) runs in a `finally` block so it fires
    on every exit path, not just a clean `StreamEnd`:

    - A clean finish (`StreamEnd` reached) logs `outcome="ok"` with the
      provider's authoritative token counts.
    - A provider error mid-stream surfaces as an in-band SSE error event
      (as before) and logs `outcome="provider_error"` with tokens estimated
      from the accumulated delta text via `estimate_tokens`, since no
      authoritative count exists without a `StreamEnd`.
    - A client disconnect (`GeneratorExit`/`asyncio.CancelledError`, neither
      an `Exception` subclass) logs `outcome="client_disconnect"` the same
      estimated way, then re-raises - a disconnected client cannot receive
      the closing chunk or an error event either way, so the fix records the
      row without attempting to resurrect the connection.
    - A stream whose iteration ends without ever reaching `StreamEnd` (the
      openai/google/ollama providers all emit it conditionally, so their
      generators can finish without it) is a *successful* completion: the
      client received the full body, so the row is logged `outcome="ok"`.
      Only the authoritative token count is missing, so tokens are estimated
      from the accumulated delta text (the same `estimate_tokens` fallback the
      failure paths use) rather than left at $0, and the terminal chunk the
      provider never sent is synthesized so the client still sees a
      well-formed stream. Tagging this `provider_error` instead would surface
      a phantom error event to a client that received a correct response and
      would deflate the success rate for a request that actually succeeded.

    Uses its own DB session (`SessionLocal`) since this generator keeps
    running after the request-scoped session dependency has already been
    closed. The accounting write in `finally` is wrapped in `_run_shielded`
    because a disconnecting client can inject cancellation more than once
    while that write is in flight, and a bare `await` there could let a
    second cancellation cut the DB commit short.

    `state` is `request.scope["state"]`, passed in because the generator runs
    after the endpoint has returned and can no longer reach the `request`
    object itself. It doubles as the channel back to the middleware:
    `StreamTimer.finish()` writes `provider_ms` onto it so the middleware can
    derive `gateway_overhead_seconds` once the stream closes, on every exit
    path including a failed one. The middleware still records end-to-end for
    this request; what the generator adds is TTFT, inter-token gaps, and
    time-to-last-token. Timing is recorded via StreamTimer and lands on the
    same RequestLog row.
    """
    completion_id = new_completion_id()
    created = int(time.time())
    timer = StreamTimer(state, model=model)

    outcome = "ok"
    input_tokens = output_tokens = 0
    accumulated: list[str] = []
    stream_ended = False
    try:
        yield _event(role_chunk(id=completion_id, created=created, model=model))
        timer.provider_started()
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                timer.delta()
                accumulated.append(ev.text)
                yield _event(text_chunk(ev.text, id=completion_id, created=created, model=model))
            elif isinstance(ev, StreamEnd):
                stream_ended = True
                outcome = "ok"
                input_tokens, output_tokens = ev.input_tokens, ev.output_tokens
                yield _event(
                    final_chunk(ev.stop_reason, id=completion_id, created=created, model=model)
                )
                break
        if not stream_ended:
            # The provider finished a complete stream without emitting a
            # StreamEnd - the openai/google/ollama providers all gate it
            # conditionally, so their generators legitimately end without it.
            # The client received the full completion, so this is a success;
            # we only lack authoritative token counts and fall back to
            # estimates, then synthesize the terminal chunk the provider never
            # sent so the client still sees a well-formed stream.
            outcome = "ok"
            input_tokens = estimate_tokens(_payload_text(payload))
            output_tokens = estimate_tokens("".join(accumulated))
            yield _event(final_chunk(None, id=completion_id, created=created, model=model))
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_disconnect"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        raise
    except Exception as exc:  # surface upstream errors inside the stream
        outcome = "provider_error"
        input_tokens = estimate_tokens(_payload_text(payload))
        output_tokens = estimate_tokens("".join(accumulated))
        error_payload = {
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "code": "provider_error",
            }
        }
        yield f"data: {json.dumps(error_payload)}\n\n"
    finally:
        # NEVER yield here - illegal during GeneratorExit.
        timings = timer.finish(succeeded=(outcome == "ok"))
        cost_usd = calculate_cost(provider_name, model, input_tokens, output_tokens)

        async def _record() -> None:
            async with SessionLocal() as session:
                await log_request(
                    session,
                    key_id=key_id,
                    account_id=account_id,
                    provider=provider_name,
                    model=model,
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    response_id=completion_id,
                    prompt_name=prompt_name,
                    routed_from=routed_from,
                    prompt_version_num=prompt_version_num,
                    path=_STREAM_PATH,
                    outcome=outcome,
                    duration_ms=timings.duration_ms,
                    provider_ms=timings.provider_ms,
                    ttft_ms=timings.ttft_ms,
                )
            observe_request(
                model=model,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                cost_usd=cost_usd,
            )

        await _run_shielded(_record())
    # Unreachable on the client-disconnect path: the `raise` above
    # re-propagates once `finally` completes, so control never reaches here.
    yield "data: [DONE]\n\n"


def _event(chunk) -> str:
    """Format a ChatCompletionChunk as one SSE `data:` event."""
    return f"data: {chunk.model_dump_json()}\n\n"
