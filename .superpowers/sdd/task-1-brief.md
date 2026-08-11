### Task 1: Project scaffolding & configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `gatekeep/__init__.py`
- Create: `gatekeep/config.py`
- Create: `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `gatekeep.config.Settings` (pydantic-settings model) and `gatekeep.config.get_settings() -> Settings` (cached). Fields: `database_url: str`, `redis_url: str`, `anthropic_api_key: str`, `default_model: str = "claude-sonnet-5"`, `default_max_tokens: int = 4096`, `model_aliases: dict[str, str]`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "gatekeep"
version = "0.1.0"
description = "Self-hosted OpenAI-compatible LLM gateway with prompt-eval gating"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "sqlalchemy[asyncio]>=2.0",
    "asyncpg>=0.29",
    "alembic>=1.13",
    "pydantic>=2.7",
    "pydantic-settings>=2.3",
    "anthropic>=0.40",
    "redis>=5.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
    "pytest-asyncio>=0.23",
    "httpx>=0.27",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["gatekeep*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Write `.gitignore`**

```gitignore
__pycache__/
*.py[cod]
.venv/
venv/
*.egg-info/
.env
.pytest_cache/
```

- [ ] **Step 3: Write `.env.example`**

```dotenv
# Postgres (async driver)
DATABASE_URL=postgresql+asyncpg://gatekeep:gatekeep@localhost:5432/gatekeep
# Redis (used from Phase 2 onward)
REDIS_URL=redis://localhost:6379/0
# Anthropic API key the gateway uses to call Claude
ANTHROPIC_API_KEY=sk-ant-your-key-here
# Default Claude model when the client sends an unknown model id
DEFAULT_MODEL=claude-sonnet-5
DEFAULT_MAX_TOKENS=4096
```

- [ ] **Step 4: Create empty `gatekeep/__init__.py` and `tests/__init__.py`**

Both files are empty.

- [ ] **Step 5: Write the failing test `tests/test_config.py`**

```python
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
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pip install -e ".[dev]" && pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.config'` (or ImportError for `Settings`).

- [ ] **Step 7: Write `gatekeep/config.py`**

```python
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    anthropic_api_key: str
    default_model: str = "claude-sonnet-5"
    default_max_tokens: int = 4096
    model_aliases: dict[str, str] = Field(
        default_factory=lambda: {
            "gpt-4": "claude-sonnet-5",
            "gpt-4o": "claude-sonnet-5",
            "gpt-4o-mini": "claude-haiku-4-5",
            "gpt-3.5-turbo": "claude-haiku-4-5",
        }
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 passed).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore .env.example gatekeep/__init__.py gatekeep/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: project scaffolding and settings"
```

---

