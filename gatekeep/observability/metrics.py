from __future__ import annotations

from prometheus_client import Counter, Histogram

# key_id is unbounded (one value per API key, never reclaimed) and would
# multiply badly against histogram buckets, so it is deliberately excluded
# here and everywhere else in this module. Per-key breakdowns come from
# Postgres via /dashboard/api/usage/summary instead.
requests_total = Counter(
    "gatekeep_requests_total",
    "Total number of chat completion requests.",
    ["model"],
)

request_tokens = Histogram(
    "gatekeep_request_tokens",
    "Prompt plus completion tokens per request.",
    ["model"],
)

request_cost_usd = Histogram(
    "gatekeep_request_cost_usd",
    "USD cost per request.",
    ["model"],
)

rate_limit_rejections_total = Counter(
    "gatekeep_rate_limit_rejections_total",
    "Requests rejected with 429 for exceeding a key's rate limit.",
)

cache_exact_hits = Counter(
    "gatekeep_cache_exact_hits",
    "Exact-match cache hits.",
    ["model"],
)

cache_exact_misses = Counter(
    "gatekeep_cache_exact_misses",
    "Exact-match cache misses.",
    ["model"],
)

cache_semantic_hits = Counter(
    "gatekeep_cache_semantic_hits",
    "Semantic cache hits.",
    ["model"],
)

cache_semantic_misses = Counter(
    "gatekeep_cache_semantic_misses",
    "Semantic cache misses.",
    ["model"],
)

cache_semantic_similarity = Histogram(
    "gatekeep_cache_semantic_similarity",
    "Similarity score of the best semantic cache match found.",
    ["model"],
)

cache_cost_saved_usd = Counter(
    "gatekeep_cache_cost_saved_usd",
    "Cumulative USD cost saved by serving cache hits instead of calling the provider.",
)

budget_alerts_total = Counter(
    "gatekeep_budget_alerts_total",
    "Budget threshold alerts fired, by threshold level ('warning' or 'exceeded').",
    ["threshold"],
)

# Requests whose resolved model has no configured price on a billed provider,
# by the `pricing_miss_policy` outcome applied ('rejected', 'ceiling', or
# 'served_zero'). Deliberately NOT labeled by model: an unpriced model id is
# attacker-controllable, and a model label here would let a client explode this
# counter's cardinality. The specific model name is logged instead, so an
# operator still knows what to add to the pricing table.
unpriced_model_total = Counter(
    "gatekeep_unpriced_model_total",
    "Requests for a model with no configured pricing on a billed provider, by policy outcome.",
    ["provider", "outcome"],
)


def observe_request(
    model: str, prompt_tokens: int, completion_tokens: int, cost_usd: float
) -> None:
    """Record the token-count and cost histograms for one completed request."""
    labels = {"model": model}
    request_tokens.labels(**labels).observe(prompt_tokens + completion_tokens)
    request_cost_usd.labels(**labels).observe(cost_usd)


# Latency buckets. prometheus_client's defaults top out at 10s, which cannot
# describe LLM traffic. The wide set's low end matters because cache hits
# return in single-digit milliseconds and would otherwise all land in one
# bucket with provider calls.
LATENCY_BUCKETS_WIDE = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2,
    5,
    10,
    20,
    30,
    60,
    120,
)
LATENCY_BUCKETS_TIGHT = (0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2)

# `path` is one of "cache_exact", "cache_semantic", "provider", "stream". One
# label replaces separate `cached`/`streaming` labels: streaming returns before
# any cache lookup runs (app.py), so the two can never co-occur.
#
# One definition on every path: the full ASGI span, written only by
# LatencyMiddleware. Aggregating across paths is therefore meaningful, though
# the distributions differ wildly (a cache hit is milliseconds, a stream is
# however long generation takes), so pinning `path` is usually still what you
# want. Streaming's start-until-last-token lives in
# time_to_last_token_seconds, deliberately under a different metric name.
request_duration_seconds = Histogram(
    "gatekeep_request_duration_seconds",
    "End-to-end request latency in seconds.",
    ["model", "path"],
    buckets=LATENCY_BUCKETS_WIDE,
)

provider_duration_seconds = Histogram(
    "gatekeep_provider_duration_seconds",
    "Time spent in the upstream provider call, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_WIDE,
)

# Not derived in PromQL: subtracting two histograms is not a statistically
# valid operation. On a cache hit there is no provider call, so the whole
# duration is gateway time and is recorded here in full.
gateway_overhead_seconds = Histogram(
    "gatekeep_gateway_overhead_seconds",
    "Request time not spent in the upstream provider, in seconds.",
    ["model", "path"],
    buckets=LATENCY_BUCKETS_WIDE,
)

ttft_seconds = Histogram(
    "gatekeep_ttft_seconds",
    "Time to first token on a streamed completion, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_TIGHT,
)

# Providers do not guarantee one token per delta (Anthropic's text_stream
# yields text pieces; Ollama yields per-token), so this is really inter-chunk
# latency in provider-native units. The token-normalized figure is the
# DB-derived mean: (duration_ms - ttft_ms) / NULLIF(completion_tokens - 1, 0).
inter_token_seconds = Histogram(
    "gatekeep_inter_token_seconds",
    "Gap between consecutive streamed text deltas, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_TIGHT,
)

# Streaming-only, and deliberately separate from request_duration_seconds: the
# span from request start to the last token is a different quantity from the
# full ASGI span, and folding both into one metric under different label values
# makes any unpinned aggregation meaningless. Wide buckets, because this scales
# with generation length. Pairs with ttft_seconds and inter_token_seconds, which
# are likewise (model,)-labeled and streaming-only.
time_to_last_token_seconds = Histogram(
    "gatekeep_time_to_last_token_seconds",
    "Request start until the last streamed token, in seconds.",
    ["model"],
    buckets=LATENCY_BUCKETS_WIDE,
)
