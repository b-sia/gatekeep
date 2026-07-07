from gatekeep.app import get_provider
from gatekeep.config import get_settings
from gatekeep.providers.anthropic import AnthropicProvider
from gatekeep.providers.ollama import OllamaProvider


def _set_common_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def test_get_provider_returns_anthropic_by_default(monkeypatch):
    _set_common_env(monkeypatch)
    get_settings.cache_clear()
    provider = get_provider()
    assert isinstance(provider, AnthropicProvider)


def test_get_provider_returns_ollama_when_configured(monkeypatch):
    _set_common_env(monkeypatch)
    monkeypatch.setenv("PROVIDER", "ollama")
    get_settings.cache_clear()
    provider = get_provider()
    assert isinstance(provider, OllamaProvider)
