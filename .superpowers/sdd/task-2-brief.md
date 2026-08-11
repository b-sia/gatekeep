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

