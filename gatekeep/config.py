from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway configuration, loaded from the environment or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    anthropic_api_key: str
    ollama_host: str = "http://localhost:11434"
    default_model: str = "claude-sonnet-5"
    default_max_tokens: int = 4096
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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()
