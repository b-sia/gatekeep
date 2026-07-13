from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

requests_total = Counter(
    "gatekeep_requests_total",
    "Total number of chat completion requests.",
    ["model", "key_id"],
)

request_tokens = Histogram(
    "gatekeep_request_tokens",
    "Prompt plus completion tokens per request.",
    ["model", "key_id"],
)

request_cost_usd = Histogram(
    "gatekeep_request_cost_usd",
    "USD cost per request.",
    ["model", "key_id"],
)

rate_limit_remaining = Gauge(
    "gatekeep_rate_limit_remaining",
    "Tokens remaining in a key's rate-limit bucket after the last check.",
    ["key_id"],
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


def observe_request(
    model: str, key_id: int, prompt_tokens: int, completion_tokens: int, cost_usd: float
) -> None:
    """Record the token-count and cost histograms for one completed request."""
    labels = {"model": model, "key_id": str(key_id)}
    request_tokens.labels(**labels).observe(prompt_tokens + completion_tokens)
    request_cost_usd.labels(**labels).observe(cost_usd)
