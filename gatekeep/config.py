from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway configuration, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    anthropic_api_key: str
    ollama_host: str = "http://localhost:11434"
    openai_api_key: str | None = None
    google_api_key: str | None = None
    default_model: str = "claude-sonnet-5"
    default_max_tokens: int = 4096
    # Optional path to an operator-supplied pricing override file (same
    # {"models": {"<provider>/<model>": {...}}} shape as the vendored
    # gatekeep/data/model_prices.json). Entries here win over the vendored
    # baseline, letting an operator price preview/self-hosted models no public
    # dataset covers. None (default) means baseline-only. See gatekeep/pricing.py.
    pricing_overrides_path: str | None = None
    # What to do with a request whose resolved model has no configured price on
    # a billed provider (anthropic/openai/google). Ollama is self-hosted and
    # genuinely free, so this never applies to it.
    #   "reject" (default): refuse the request with a 400 before the upstream
    #       call - a model gatekeep cannot price is a model it cannot govern.
    #   "ceiling": serve it, but bill it at `pricing_ceiling_per_1m` so budgets
    #       clamp down rather than open up, and emit an alert.
    #   "alert_zero": serve it at $0 (the old fail-open behavior) but emit an
    #       alert so the gap is at least visible.
    # See gatekeep/accounting.enforce_pricing_policy.
    pricing_miss_policy: Literal["reject", "ceiling", "alert_zero"] = "reject"
    # Per-1M-token USD price charged to an unpriced billed-provider model when
    # `pricing_miss_policy` is "ceiling" (applied to both input and output).
    pricing_ceiling_per_1m: float = 100.0
    model_aliases: dict[str, str] = Field(
        default_factory=lambda: {
            "gpt-4": "claude-sonnet-5",
            "gpt-4o": "claude-sonnet-5",
            "gpt-4o-mini": "claude-haiku-4-5-20251001",
            "gpt-3.5-turbo": "claude-haiku-4-5-20251001",
        }
    )
    rate_limit_tokens_per_min: int = 100
    rate_limit_refill_rate: float = 100 / 60
    cache_exact_ttl_seconds: int = 604800
    semantic_cache_similarity_threshold: float = 0.95
    cache_purge_interval_seconds: int = 3600
    eval_judge_model: str = "claude-sonnet-5"
    eval_pass_threshold_default: float = 0.9
    # Fraction of a key's monthly_budget_usd at which a "warning" alert fires
    # (in addition to the "exceeded" alert fired at/above 100%). Purely
    # observational - does not affect enforcement, which always blocks at
    # spend >= monthly_budget_usd regardless of this setting.
    budget_alert_threshold: float = 0.8
    # How often (seconds) the background job reconciles every account's
    # Redis spend counter against the request_logs DB aggregate, overwriting
    # any drift from a lost record_spend increment or INCRBYFLOAT rounding.
    # See gatekeep.middleware.budget.run_budget_reconciliation_loop.
    budget_reconcile_interval_seconds: int = 3600


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
