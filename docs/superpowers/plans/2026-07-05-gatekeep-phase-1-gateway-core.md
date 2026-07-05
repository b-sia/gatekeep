# gatekeep Phase 1 — Gateway Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working, self-hosted, OpenAI-compatible HTTP gateway that authenticates a client by API key and proxies `POST /v1/chat/completions` (streaming and non-streaming) to Anthropic's Messages API.

**Architecture:** A single async FastAPI service. Requests arrive in OpenAI Chat Completions schema, are authenticated against an `api_keys` table in Postgres, translated to the Anthropic Messages format, sent to Claude via the async Anthropic SDK, and translated back to OpenAI schema. A pure (SDK-free) translation layer keeps the mapping unit-testable; a thin provider wrapper isolates the SDK. Postgres and Redis run via docker-compose (Redis is provisioned now for later phases but unused in Phase 1).

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, SQLAlchemy 2.x (async) + asyncpg, Alembic, pydantic / pydantic-settings, the `anthropic` async SDK, pytest + pytest-asyncio + httpx. Postgres 16 (pgvector image, for later phases), Redis 7.

## Global Constraints

_Every task's requirements implicitly include this section._

- **Python:** 3.11 or newer.
- **Package name / import root:** `gatekeep`. CLI command (later phase): `gatekeep`.
- **Model IDs are exact strings — never append date suffixes:** `claude-sonnet-5` (default), `claude-haiku-4-5`, `claude-opus-4-8`. If a client sends an unknown model, resolve to the configured default.
- **Do NOT forward `temperature`, `top_p`, or `top_k` to Anthropic.** Sonnet 5 / Opus 4.8 reject non-default sampling parameters with HTTP 400. The translation layer drops them.
- **Anthropic requests require `max_tokens`.** If the client omits it, use `settings.default_max_tokens`.
- **Secrets come from the environment**, never hardcoded. Config is loaded via `pydantic-settings` from env / `.env`.
- **Every task ends on a green test run and a commit.**

---

## File Structure

```
gatekeep/
├── pyproject.toml                     # project metadata + deps
├── .env.example                       # documented env vars
├── .gitignore
├── Dockerfile                         # gateway image
├── docker-compose.yml                 # gateway + postgres(pgvector) + redis
├── alembic.ini                        # alembic config
├── migrations/                        # alembic migration env + versions
│   ├── env.py
│   └── versions/0001_api_keys.py
├── gatekeep/
│   ├── __init__.py
│   ├── config.py                      # Settings (pydantic-settings)
│   ├── db.py                          # async engine + session factory + get_session
│   ├── models.py                      # SQLAlchemy Base + ApiKey
│   ├── auth_keys.py                   # hash_key(), generate_key()
│   ├── api/
│   │   ├── __init__.py
│   │   ├── openai_schemas.py          # OpenAI request/response/chunk pydantic models
│   │   ├── translation.py             # pure OpenAI<->Anthropic mapping
│   │   └── errors.py                  # OpenAI-shaped error responses + mapper
│   ├── providers/
│   │   ├── __init__.py
│   │   └── anthropic.py               # AnthropicProvider + normalized result types
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── auth.py                     # require_api_key FastAPI dependency
│   └── app.py                          # FastAPI app, DI wiring, endpoints
├── scripts/
│   └── create_key.py                   # insert an api key, print the raw key once
└── tests/
    ├── __init__.py
    ├── conftest.py                     # async fixtures (db session, http client)
    ├── test_config.py
    ├── test_db.py
    ├── test_models.py
    ├── test_openai_schemas.py
    ├── test_translation.py
    ├── test_provider.py
    ├── test_auth.py
    └── test_endpoint.py
```

---

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

### Task 2: Docker Compose stack + async DB engine

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `gatekeep/db.py`
- Test: `tests/test_db.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: `gatekeep.config.get_settings`.
- Produces: `gatekeep.db.engine` (AsyncEngine), `gatekeep.db.SessionLocal` (async_sessionmaker), `gatekeep.db.get_session()` (async generator dependency yielding `AsyncSession`), and `gatekeep.db.Base` re-export.

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY gatekeep ./gatekeep
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "gatekeep.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: gatekeep
      POSTGRES_PASSWORD: gatekeep
      POSTGRES_DB: gatekeep
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U gatekeep"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:7
    ports:
      - "6379:6379"

  gateway:
    build: .
    environment:
      DATABASE_URL: postgresql+asyncpg://gatekeep:gatekeep@postgres:5432/gatekeep
      REDIS_URL: redis://redis:6379/0
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
```

- [ ] **Step 3: Write `gatekeep/db.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from gatekeep.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(get_settings().database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 4: Write `tests/conftest.py`** (shared async DB session fixture used by later tasks too)

```python
import pytest_asyncio
from sqlalchemy import text

from gatekeep.db import Base, SessionLocal, engine


@pytest_asyncio.fixture(autouse=True)
async def _create_schema():
    # Import models so their tables register on Base.metadata.
    import gatekeep.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def db_ping():
    async with SessionLocal() as s:
        result = await s.execute(text("SELECT 1"))
        return result.scalar_one()
```

- [ ] **Step 5: Write the failing test `tests/test_db.py`**

```python
async def test_database_reachable(db_ping):
    assert db_ping == 1
```

- [ ] **Step 6: Start Postgres, then run the test to verify it fails first for the right reason**

Run:
```bash
cp -n .env.example .env   # ensure a .env exists for local runs
docker compose up -d postgres redis
pytest tests/test_db.py -v
```
Expected before `gatekeep/models.py` exists (Task 3): FAIL with `ModuleNotFoundError: No module named 'gatekeep.models'` raised inside the `_create_schema` fixture. This confirms the fixture wiring; the connection itself is exercised once Task 3 lands. If Postgres is not up you will instead see a connection error — start it first.

- [ ] **Step 7: Create a placeholder `gatekeep/models.py` so the fixture imports**

```python
from gatekeep.db import Base  # noqa: F401
```

- [ ] **Step 8: Run the test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: PASS (1 passed) — proves the async engine connects to the running Postgres.

- [ ] **Step 9: Commit**

```bash
git add Dockerfile docker-compose.yml gatekeep/db.py gatekeep/models.py tests/conftest.py tests/test_db.py
git commit -m "feat: docker-compose stack and async db engine"
```

---

### Task 3: Data model, key hashing, migrations

**Files:**
- Modify: `gatekeep/models.py`
- Create: `gatekeep/auth_keys.py`
- Create: `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_api_keys.py`
- Create: `scripts/create_key.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `gatekeep.db.Base`, `gatekeep.db.SessionLocal`.
- Produces:
  - `gatekeep.models.ApiKey` with columns `id: int` (pk), `name: str`, `key_hash: str` (unique), `active: bool = True`, `created_at: datetime`.
  - `gatekeep.auth_keys.generate_key() -> str` (raw key, prefix `gk-`), `gatekeep.auth_keys.hash_key(raw: str) -> str` (sha256 hex).

- [ ] **Step 1: Write `gatekeep/auth_keys.py`**

```python
from __future__ import annotations

import hashlib
import secrets


def generate_key() -> str:
    return "gk-" + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 2: Write the failing test `tests/test_models.py`**

```python
from sqlalchemy import select

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey


def test_generate_and_hash_are_stable():
    raw = generate_key()
    assert raw.startswith("gk-")
    assert hash_key(raw) == hash_key(raw)
    assert hash_key(raw) != hash_key(generate_key())


async def test_api_key_persists(session):
    raw = generate_key()
    session.add(ApiKey(name="test", key_hash=hash_key(raw)))
    await session.commit()

    found = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one()
    assert found.name == "test"
    assert found.active is True
    assert found.created_at is not None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'ApiKey' from 'gatekeep.models'`.

- [ ] **Step 4: Replace `gatekeep/models.py` with the real model**

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from gatekeep.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 6: Write `alembic.ini`**

```ini
[alembic]
script_location = migrations
sqlalchemy.url = driver://user:pass@localhost/dbname

[loggers]
keys = root

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
```

- [ ] **Step 7: Write `migrations/env.py`** (offline/online, uses a sync psycopg URL derived from settings so Alembic runs standalone)

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from gatekeep.config import get_settings
from gatekeep.db import Base
import gatekeep.models  # noqa: F401  (register tables)

config = context.config

# Alembic runs synchronously; convert the async URL to a sync psycopg2 one.
sync_url = get_settings().database_url.replace("+asyncpg", "")
config.set_main_option("sqlalchemy.url", sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 8: Write `migrations/versions/0001_api_keys.py`**

```python
"""api_keys table

Revision ID: 0001
Revises:
Create Date: 2026-07-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
```

Note: Alembic's sync URL uses psycopg2. Add `psycopg2-binary` to dev deps if not present — install it now: `pip install psycopg2-binary`.

- [ ] **Step 9: Run the migration against the running Postgres to verify it applies**

Run:
```bash
alembic upgrade head
```
Expected: output ending in `Running upgrade  -> 0001, api_keys table` with no error. (Tests use `create_all` and don't depend on Alembic; this step verifies the migration path used by docker-compose deploys.)

- [ ] **Step 10: Write `scripts/create_key.py`**

```python
"""Insert an API key and print the raw key exactly once.

Usage: python scripts/create_key.py "my client name"
"""

import asyncio
import sys

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.db import SessionLocal
from gatekeep.models import ApiKey


async def main(name: str) -> None:
    raw = generate_key()
    async with SessionLocal() as session:
        session.add(ApiKey(name=name, key_hash=hash_key(raw)))
        await session.commit()
    print(raw)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('usage: python scripts/create_key.py "client name"', file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 11: Commit**

```bash
git add gatekeep/models.py gatekeep/auth_keys.py alembic.ini migrations scripts/create_key.py tests/test_models.py
git commit -m "feat: api_keys model, key hashing, and initial migration"
```

---

### Task 4: OpenAI request/response schemas

**Files:**
- Create: `gatekeep/api/__init__.py`
- Create: `gatekeep/api/openai_schemas.py`
- Test: `tests/test_openai_schemas.py`

**Interfaces:**
- Consumes: nothing.
- Produces (all pydantic `BaseModel`):
  - `ChatMessage(role: str, content: str | list[dict] | None, name: str | None)`
  - `ChatCompletionRequest(model: str, messages: list[ChatMessage], max_tokens: int | None, max_completion_tokens: int | None, temperature: float | None, top_p: float | None, stream: bool = False, stop: str | list[str] | None)`
  - `Usage(prompt_tokens, completion_tokens, total_tokens)`
  - `ResponseMessage(role="assistant", content: str | None)`
  - `Choice(index, message: ResponseMessage, finish_reason: str | None)`
  - `ChatCompletionResponse(id, object="chat.completion", created, model, choices, usage)`
  - `DeltaMessage(role: str | None, content: str | None)`
  - `ChunkChoice(index, delta: DeltaMessage, finish_reason: str | None)`
  - `ChatCompletionChunk(id, object="chat.completion.chunk", created, model, choices)`

- [ ] **Step 1: Write the failing test `tests/test_openai_schemas.py`**

```python
from gatekeep.api.openai_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ResponseMessage,
    Usage,
)


def test_parses_minimal_request():
    req = ChatCompletionRequest.model_validate(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert req.model == "gpt-4o"
    assert req.stream is False
    assert req.messages[0].content == "hi"


def test_response_serializes_openai_shape():
    resp = ChatCompletionResponse(
        id="chatcmpl-x",
        created=1,
        model="claude-sonnet-5",
        choices=[Choice(message=ResponseMessage(content="hello"), finish_reason="stop")],
        usage=Usage(prompt_tokens=3, completion_tokens=1, total_tokens=4),
    )
    data = resp.model_dump()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_openai_schemas.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.api'`.

- [ ] **Step 3: Create empty `gatekeep/api/__init__.py`, then write `gatekeep/api/openai_schemas.py`**

```python
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: Union[str, list[dict[str, Any]], None] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str
    messages: list[ChatMessage]
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stream: bool = False
    stop: Union[str, list[str], None] = None


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: Optional[str] = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


class DeltaMessage(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChunkChoice]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_openai_schemas.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/__init__.py gatekeep/api/openai_schemas.py tests/test_openai_schemas.py
git commit -m "feat: openai-compatible request/response schemas"
```

---

### Task 5: Translation layer (pure, SDK-free)

**Files:**
- Create: `gatekeep/api/translation.py`
- Test: `tests/test_translation.py`

**Interfaces:**
- Consumes: `gatekeep.api.openai_schemas` models; `gatekeep.providers.anthropic.CompletionResult` (defined in Task 6 — see the shape below and keep names identical).
- Produces:
  - `resolve_model(requested: str, *, default_model: str, aliases: dict[str, str]) -> str`
  - `openai_to_anthropic(req: ChatCompletionRequest, *, default_max_tokens: int, default_model: str, model_aliases: dict[str, str]) -> dict[str, Any]` — returns a kwargs dict for `messages.create`/`.stream` containing `model`, `messages`, `max_tokens`, optionally `system` and `stop_sequences`. Never contains `temperature`/`top_p`/`top_k`.
  - `result_to_openai(result: CompletionResult, *, model: str) -> ChatCompletionResponse`
  - `role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk` (initial assistant-role delta)
  - `text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk`
  - `final_chunk(stop_reason: str | None, *, id: str, created: int, model: str) -> ChatCompletionChunk`
  - `FINISH_REASON_MAP: dict[str, str]`
  - `class TranslationError(ValueError)`

The `CompletionResult` shape this task assumes (Task 6 defines it): a dataclass with `text: str`, `input_tokens: int`, `output_tokens: int`, `stop_reason: str | None`.

- [ ] **Step 1: Write the failing test `tests/test_translation.py`**

```python
from dataclasses import dataclass

import pytest

from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    final_chunk,
    openai_to_anthropic,
    resolve_model,
    result_to_openai,
    role_chunk,
    text_chunk,
)


@dataclass
class FakeResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


ALIASES = {"gpt-4o": "claude-sonnet-5"}


def _req(**kw):
    base = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return ChatCompletionRequest.model_validate(base)


def test_resolve_model_alias_passthrough_default():
    assert resolve_model("gpt-4o", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-sonnet-5"
    assert resolve_model("claude-opus-4-8", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-opus-4-8"
    assert resolve_model("mystery", default_model="claude-sonnet-5", aliases=ALIASES) == "claude-sonnet-5"


def test_system_message_lifted_and_sampling_dropped():
    req = _req(
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        temperature=0.9,
        top_p=0.5,
        max_tokens=100,
    )
    payload = openai_to_anthropic(
        req, default_max_tokens=4096, default_model="claude-sonnet-5", model_aliases=ALIASES
    )
    assert payload["system"] == "be terse"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["max_tokens"] == 100
    assert payload["model"] == "claude-sonnet-5"
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_default_max_tokens_applied():
    payload = openai_to_anthropic(
        _req(), default_max_tokens=777, default_model="claude-sonnet-5", model_aliases=ALIASES
    )
    assert payload["max_tokens"] == 777


def test_no_conversational_message_raises():
    req = _req(messages=[{"role": "system", "content": "only system"}])
    with pytest.raises(TranslationError):
        openai_to_anthropic(
            req, default_max_tokens=10, default_model="claude-sonnet-5", model_aliases=ALIASES
        )


def test_result_to_openai_maps_usage_and_finish_reason():
    result = FakeResult(text="hello", input_tokens=3, output_tokens=2, stop_reason="end_turn")
    resp = result_to_openai(result, model="claude-sonnet-5")
    assert resp.choices[0].message.content == "hello"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.prompt_tokens == 3
    assert resp.usage.completion_tokens == 2
    assert resp.usage.total_tokens == 5
    assert resp.id.startswith("chatcmpl-")


def test_stream_chunk_helpers():
    rc = role_chunk(id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert rc.choices[0].delta.role == "assistant"
    tc = text_chunk("hi", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert tc.choices[0].delta.content == "hi"
    fc = final_chunk("max_tokens", id="chatcmpl-1", created=1, model="claude-sonnet-5")
    assert fc.choices[0].finish_reason == "length"
    assert fc.choices[0].delta.content is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_translation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.api.translation'`.

- [ ] **Step 3: Write `gatekeep/api/translation.py`**

```python
from __future__ import annotations

import time
import uuid
from typing import Any

from gatekeep.api.openai_schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    ChunkChoice,
    DeltaMessage,
    ResponseMessage,
    Usage,
)

FINISH_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class TranslationError(ValueError):
    """Raised when an OpenAI request cannot be mapped to Anthropic."""


def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def resolve_model(requested: str, *, default_model: str, aliases: dict[str, str]) -> str:
    if requested in aliases:
        return aliases[requested]
    if requested.startswith("claude-"):
        return requested
    return default_model


def openai_to_anthropic(
    req: ChatCompletionRequest,
    *,
    default_max_tokens: int,
    default_model: str,
    model_aliases: dict[str, str],
) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for msg in req.messages:
        text = _extract_text(msg.content)
        if msg.role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": text})
        else:  # "tool" and anything else
            raise TranslationError(f"unsupported message role in v1: {msg.role}")

    if not messages:
        raise TranslationError("request must contain at least one user or assistant message")

    payload: dict[str, Any] = {
        "model": resolve_model(req.model, default_model=default_model, aliases=model_aliases),
        "messages": messages,
        "max_tokens": req.max_tokens or req.max_completion_tokens or default_max_tokens,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if req.stop:
        payload["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else list(req.stop)
    # temperature/top_p/top_k intentionally omitted (rejected by Sonnet 5 / Opus 4.8).
    return payload


def new_completion_id() -> str:
    return "chatcmpl-" + uuid.uuid4().hex


def result_to_openai(result: Any, *, model: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                index=0,
                message=ResponseMessage(content=result.text),
                finish_reason=FINISH_REASON_MAP.get(result.stop_reason, "stop"),
            )
        ],
        usage=Usage(
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            total_tokens=result.input_tokens + result.output_tokens,
        ),
    )


def role_chunk(*, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(role="assistant"))],
    )


def text_chunk(text: str, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[ChunkChoice(index=0, delta=DeltaMessage(content=text))],
    )


def final_chunk(stop_reason: str | None, *, id: str, created: int, model: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id=id,
        created=created,
        model=model,
        choices=[
            ChunkChoice(
                index=0,
                delta=DeltaMessage(),
                finish_reason=FINISH_REASON_MAP.get(stop_reason, "stop"),
            )
        ],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_translation.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/translation.py tests/test_translation.py
git commit -m "feat: pure openai<->anthropic translation layer"
```

---

### Task 6: Anthropic provider wrapper

**Files:**
- Create: `gatekeep/providers/__init__.py`
- Create: `gatekeep/providers/anthropic.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- Consumes: the `anthropic` async SDK client (injected — not constructed here, so tests use a fake).
- Produces:
  - `@dataclass CompletionResult(text: str, input_tokens: int, output_tokens: int, stop_reason: str | None)`
  - `@dataclass TextDelta(text: str)`
  - `@dataclass StreamEnd(stop_reason: str | None, input_tokens: int, output_tokens: int)`
  - `class AnthropicProvider(client)` with `async def complete(payload: dict) -> CompletionResult` and `async def stream(payload: dict) -> AsyncIterator[TextDelta | StreamEnd]`.

- [ ] **Step 1: Write the failing test `tests/test_provider.py`** (fakes model the SDK's shapes)

```python
from types import SimpleNamespace

from gatekeep.providers.anthropic import (
    AnthropicProvider,
    CompletionResult,
    StreamEnd,
    TextDelta,
)


class FakeMessages:
    async def create(self, **payload):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="hello world")],
            usage=SimpleNamespace(input_tokens=5, output_tokens=2),
            stop_reason="end_turn",
        )

    def stream(self, **payload):
        return FakeStream()


class FakeStream:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    async def text_stream(self):  # not used; iteration below via __aiter__ shim
        raise NotImplementedError

    def __aiter__(self):
        raise NotImplementedError


class FakeStreamCtx:
    """Async context manager whose .text_stream yields deltas."""

    def __init__(self):
        self._deltas = ["hel", "lo"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self):
        return SimpleNamespace(
            usage=SimpleNamespace(input_tokens=4, output_tokens=2),
            stop_reason="max_tokens",
        )


class FakeMessagesStreaming(FakeMessages):
    def stream(self, **payload):
        return FakeStreamCtx()


class FakeClient:
    def __init__(self, messages):
        self.messages = messages


async def test_complete_returns_normalized_result():
    provider = AnthropicProvider(FakeClient(FakeMessages()))
    result = await provider.complete({"model": "claude-sonnet-5", "messages": [], "max_tokens": 10})
    assert isinstance(result, CompletionResult)
    assert result.text == "hello world"
    assert result.input_tokens == 5
    assert result.output_tokens == 2
    assert result.stop_reason == "end_turn"


async def test_stream_yields_deltas_then_end():
    provider = AnthropicProvider(FakeClient(FakeMessagesStreaming()))
    events = [e async for e in provider.stream({"model": "claude-sonnet-5", "messages": [], "max_tokens": 10})]
    deltas = [e for e in events if isinstance(e, TextDelta)]
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert "".join(d.text for d in deltas) == "hello"
    assert len(ends) == 1
    assert ends[0].stop_reason == "max_tokens"
    assert ends[0].output_tokens == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.providers'`.

- [ ] **Step 3: Create empty `gatekeep/providers/__init__.py`, then write `gatekeep/providers/anthropic.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None


@dataclass
class TextDelta:
    text: str


@dataclass
class StreamEnd:
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class AnthropicProvider:
    """Thin async wrapper over the Anthropic SDK client.

    The client is injected so the mapping is testable with a fake.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        message = await self._client.messages.create(**payload)
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return CompletionResult(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            stop_reason=message.stop_reason,
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        async with self._client.messages.stream(**payload) as stream:
            async for text in stream.text_stream:
                yield TextDelta(text=text)
            final = await stream.get_final_message()
            yield StreamEnd(
                stop_reason=final.stop_reason,
                input_tokens=final.usage.input_tokens,
                output_tokens=final.usage.output_tokens,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_provider.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gatekeep/providers/__init__.py gatekeep/providers/anthropic.py tests/test_provider.py
git commit -m "feat: anthropic provider wrapper with normalized result types"
```

---

### Task 7: API-key auth dependency + OpenAI error responses

**Files:**
- Create: `gatekeep/api/errors.py`
- Create: `gatekeep/middleware/__init__.py`
- Create: `gatekeep/middleware/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `gatekeep.models.ApiKey`, `gatekeep.auth_keys.hash_key`, `gatekeep.db.get_session`.
- Produces:
  - `gatekeep.api.errors.openai_error(status_code: int, message: str, err_type: str, code: str | None = None) -> JSONResponse`
  - `gatekeep.api.errors.map_anthropic_error(exc) -> JSONResponse`
  - `gatekeep.middleware.auth.extract_bearer(authorization: str | None, x_api_key: str | None) -> str | None`
  - `gatekeep.middleware.auth.require_api_key(...)` — FastAPI dependency returning an `ApiKey`, raising `HTTPException` (OpenAI-shaped body) on missing/invalid/inactive key.

- [ ] **Step 1: Write `gatekeep/api/errors.py`**

```python
from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status_code: int, message: str, err_type: str, code: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": code}},
    )


def map_anthropic_error(exc: Exception) -> JSONResponse:
    status = getattr(exc, "status_code", 502)
    message = getattr(exc, "message", None) or str(exc)
    return openai_error(status, message, "upstream_error", "anthropic_error")
```

- [ ] **Step 2: Write the failing test `tests/test_auth.py`**

```python
import pytest
from fastapi import HTTPException

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.middleware.auth import extract_bearer, require_api_key
from gatekeep.models import ApiKey


def test_extract_bearer_prefers_authorization():
    assert extract_bearer("Bearer abc", None) == "abc"
    assert extract_bearer(None, "xyz") == "xyz"
    assert extract_bearer(None, None) is None


async def test_require_api_key_accepts_valid(session):
    raw = generate_key()
    session.add(ApiKey(name="c", key_hash=hash_key(raw)))
    await session.commit()

    key = await require_api_key(authorization=f"Bearer {raw}", x_api_key=None, session=session)
    assert key.name == "c"


async def test_require_api_key_rejects_missing(session):
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization=None, x_api_key=None, session=session)
    assert ei.value.status_code == 401


async def test_require_api_key_rejects_unknown(session):
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization="Bearer nope", x_api_key=None, session=session)
    assert ei.value.status_code == 401


async def test_require_api_key_rejects_inactive(session):
    raw = generate_key()
    session.add(ApiKey(name="c", key_hash=hash_key(raw), active=False))
    await session.commit()
    with pytest.raises(HTTPException) as ei:
        await require_api_key(authorization=f"Bearer {raw}", x_api_key=None, session=session)
    assert ei.value.status_code == 401
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.middleware'`.

- [ ] **Step 4: Create empty `gatekeep/middleware/__init__.py`, then write `gatekeep/middleware/auth.py`**

```python
from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekeep.auth_keys import hash_key
from gatekeep.db import get_session
from gatekeep.models import ApiKey


def extract_bearer(authorization: str | None, x_api_key: str | None) -> str | None:
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            return authorization[len(prefix):].strip()
        return authorization.strip()
    if x_api_key:
        return x_api_key.strip()
    return None


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"error": {"message": message, "type": "authentication_error", "code": None}},
    )


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    session: AsyncSession = Depends(get_session),
) -> ApiKey:
    raw = extract_bearer(authorization, x_api_key)
    if not raw:
        raise _unauthorized("Missing API key. Provide 'Authorization: Bearer <key>'.")
    row = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one_or_none()
    if row is None or not row.active:
        raise _unauthorized("Invalid or inactive API key.")
    return row
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/errors.py gatekeep/middleware/__init__.py gatekeep/middleware/auth.py tests/test_auth.py
git commit -m "feat: api-key auth dependency and openai-shaped errors"
```

---

### Task 8: FastAPI app + `/v1/chat/completions` endpoint

**Files:**
- Create: `gatekeep/app.py`
- Test: `tests/test_endpoint.py`

**Interfaces:**
- Consumes: everything above — `require_api_key`, `openai_to_anthropic`, `AnthropicProvider`, translation chunk helpers, `map_anthropic_error`.
- Produces: `gatekeep.app.app` (FastAPI), `gatekeep.app.get_provider()` dependency (overridable in tests), routes `GET /healthz` and `POST /v1/chat/completions`.

- [ ] **Step 1: Write `gatekeep/app.py`**

```python
from __future__ import annotations

import time

from anthropic import AsyncAnthropic
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from gatekeep.api.errors import map_anthropic_error, openai_error
from gatekeep.api.openai_schemas import ChatCompletionRequest
from gatekeep.api.translation import (
    TranslationError,
    final_chunk,
    new_completion_id,
    openai_to_anthropic,
    result_to_openai,
    role_chunk,
    text_chunk,
)
from gatekeep.config import get_settings
from gatekeep.middleware.auth import require_api_key
from gatekeep.providers.anthropic import AnthropicProvider, StreamEnd, TextDelta

app = FastAPI(title="gatekeep")


def get_provider() -> AnthropicProvider:
    settings = get_settings()
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return AnthropicProvider(client)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    _key=Depends(require_api_key),
    provider: AnthropicProvider = Depends(get_provider),
):
    settings = get_settings()
    try:
        payload = openai_to_anthropic(
            req,
            default_max_tokens=settings.default_max_tokens,
            default_model=settings.default_model,
            model_aliases=settings.model_aliases,
        )
    except TranslationError as exc:
        return openai_error(400, str(exc), "invalid_request_error")

    model = payload["model"]

    if req.stream:
        return StreamingResponse(
            _sse(provider, payload, model),
            media_type="text/event-stream",
        )

    try:
        result = await provider.complete(payload)
    except Exception as exc:  # anthropic.APIError and friends
        return map_anthropic_error(exc)
    return JSONResponse(content=result_to_openai(result, model=model).model_dump())


async def _sse(provider: AnthropicProvider, payload: dict, model: str):
    completion_id = new_completion_id()
    created = int(time.time())
    yield _event(role_chunk(id=completion_id, created=created, model=model))
    try:
        async for ev in provider.stream(payload):
            if isinstance(ev, TextDelta):
                yield _event(text_chunk(ev.text, id=completion_id, created=created, model=model))
            elif isinstance(ev, StreamEnd):
                yield _event(
                    final_chunk(ev.stop_reason, id=completion_id, created=created, model=model)
                )
    except Exception as exc:  # surface upstream errors inside the stream
        yield f'data: {{"error": {{"message": {_json(str(exc))}, "type": "upstream_error"}}}}\n\n'
    yield "data: [DONE]\n\n"


def _event(chunk) -> str:
    return f"data: {chunk.model_dump_json()}\n\n"


def _json(s: str) -> str:
    import json

    return json.dumps(s)
```

- [ ] **Step 2: Write the failing test `tests/test_endpoint.py`** (overrides `get_provider` with a fake; uses a real DB session via ASGI transport)

```python
import httpx
import pytest_asyncio
from httpx import ASGITransport

from gatekeep.app import app, get_provider
from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import ApiKey
from gatekeep.providers.anthropic import CompletionResult, StreamEnd, TextDelta


class FakeProvider:
    async def complete(self, payload):
        assert "temperature" not in payload
        return CompletionResult(text="pong", input_tokens=3, output_tokens=1, stop_reason="end_turn")

    async def stream(self, payload):
        for t in ["po", "ng"]:
            yield TextDelta(text=t)
        yield StreamEnd(stop_reason="end_turn", input_tokens=3, output_tokens=2)


@pytest_asyncio.fixture
async def raw_key(session):
    raw = generate_key()
    session.add(ApiKey(name="c", key_hash=hash_key(raw)))
    await session.commit()
    return raw


@pytest_asyncio.fixture
async def client():
    app.dependency_overrides[get_provider] = lambda: FakeProvider()
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_requires_auth(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


async def test_non_streaming_completion(client, raw_key):
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "temperature": 0.7,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "pong"
    assert body["usage"]["total_tokens"] == 4


async def test_streaming_completion(client, raw_key):
    async with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "model": "gpt-4o",
            "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    ) as r:
        assert r.status_code == 200
        chunks = [line async for line in r.aiter_lines()]
    text = "".join(chunks)
    assert "chat.completion.chunk" in text
    assert '"content":"po"' in text
    assert "[DONE]" in text
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_endpoint.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'gatekeep.app'` (or, once the file exists but before it's correct, an assertion failure). Postgres must be running.

- [ ] **Step 4: (App already written in Step 1) Run the full suite to verify everything passes**

Run: `pytest -v`
Expected: PASS — all tests across all tasks green. Postgres and Redis must be up (`docker compose up -d postgres redis`).

- [ ] **Step 5: Manual smoke test against real Claude (optional but recommended)**

Run:
```bash
# .env must contain a real ANTHROPIC_API_KEY
docker compose up -d postgres redis
alembic upgrade head
export $(grep -v '^#' .env | xargs)
KEY=$(python scripts/create_key.py "smoke test")
uvicorn gatekeep.app:app --port 8000 &
sleep 2
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"Say hi in 3 words"}]}' | python -m json.tool
kill %1
```
Expected: a JSON `chat.completion` object whose `choices[0].message.content` is a short greeting, and `model` is `claude-sonnet-5`.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/app.py tests/test_endpoint.py
git commit -m "feat: fastapi app with /v1/chat/completions (streaming + non-streaming)"
```

---

## Phase 1 Definition of Done

- `pytest -v` is fully green with Postgres + Redis running.
- `docker compose up` brings up the gateway; `GET /healthz` returns `{"status":"ok"}`.
- A client using the OpenAI SDK with `base_url=http://localhost:8000/v1` and a `gk-` key gets real Claude completions, streaming and non-streaming.
- Sampling params are dropped; unknown models resolve to `claude-sonnet-5`; missing/invalid keys return 401 in OpenAI error shape.

## Self-Review Notes (traceability to spec)

- OpenAI-compatible `/v1/chat/completions`, stream + non-stream → Tasks 4, 5, 8.
- Translation layer incl. streaming + error mapping → Tasks 5, 7, 8.
- `providers/anthropic.py` async client with retries/streaming/usage → Task 6 (SDK provides retries/backoff by default; usage extracted in `complete`/`stream`).
- API-key auth (static keys in Postgres) → Tasks 3, 7.
- Postgres + Redis via docker-compose → Task 2 (Redis provisioned; consumed in Phase 2).
- **Deferred to later phases (correctly out of Phase 1 scope):** rate limiting, caching, cost accounting/logging, prompt registry, eval gate, curation, Prometheus/Grafana, GitHub Actions. These are Phases 2–4.
