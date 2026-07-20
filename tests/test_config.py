from gatekeep.config import Settings, get_settings


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # Isolate from whatever DEFAULT_MODEL currently happens to be in the
    # repo's local .env file, which Settings otherwise falls back to.
    monkeypatch.setenv("DEFAULT_MODEL", "claude-sonnet-5")
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


def test_ollama_host_defaults_to_localhost(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.ollama_host == "http://localhost:11434"


def test_ollama_host_reads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama-box:11434")
    get_settings.cache_clear()
    s = get_settings()
    assert s.ollama_host == "http://ollama-box:11434"


def test_openai_api_key_defaults_to_none(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert s.openai_api_key is None


def test_google_api_key_reads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "gk-google-test")
    get_settings.cache_clear()
    s = get_settings()
    assert s.google_api_key == "gk-google-test"
