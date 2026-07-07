import gatekeep.config as config_module
from gatekeep.config import Settings, get_settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    s = get_settings()
    assert isinstance(s, Settings)
    assert s.database_url.endswith("/db")
    assert s.default_model == "claude-sonnet-5"
    assert s.default_max_tokens == 4096


def test_unknown_model_alias_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.model_aliases["gpt-4"] == "claude-sonnet-5"


def test_provider_defaults_to_anthropic(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.provider == "anthropic"
    assert s.ollama_host == "http://localhost:11434"


def test_provider_reads_ollama_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama-box:11434")
    get_settings.cache_clear()
    s = get_settings()
    assert s.provider == "ollama"
    assert s.ollama_host == "http://ollama-box:11434"
