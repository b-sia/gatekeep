from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class _TestDBConfig(BaseSettings):
    """Reads DATABASE_URL/TEST_DATABASE_URL the same way gatekeep.config.Settings does."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    test_database_url: str


def _point_settings_at_test_database() -> None:
    """Redirect DATABASE_URL to TEST_DATABASE_URL before gatekeep.db creates its engine.

    `_create_schema` below drops and recreates the whole schema on every test.
    gatekeep/db.py builds its module-level engine from DATABASE_URL at import
    time, so this must run before anything imports gatekeep.db - otherwise the
    test suite tears down whatever database DATABASE_URL happens to point at
    (this bit us: alembic_version showed head with every other table gone
    because a prior test run had wiped the dev database).
    """
    config = _TestDBConfig()
    if config.test_database_url == config.database_url:
        raise RuntimeError(
            "TEST_DATABASE_URL must not be the same as DATABASE_URL - the test "
            "suite drops and recreates the schema on every run. See .env.example."
        )
    os.environ["DATABASE_URL"] = config.test_database_url


_point_settings_at_test_database()

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine.url import make_url  # noqa: E402

from gatekeep.config import get_settings  # noqa: E402
from gatekeep.db import Base, SessionLocal, engine  # noqa: E402


_database_ready = False


async def _ensure_database_exists(database_url: str) -> None:
    """Create the target Postgres database if it doesn't exist yet.

    Connects to the `postgres` maintenance database on the same server, since
    a database can't be created while connected to itself. Only does this
    once per test process (guarded by `_database_ready`) - `_create_schema`
    runs as an autouse fixture on every test, and opening a fresh maintenance
    connection per test exhausts Postgres's max_connections well before the
    suite finishes.
    """
    global _database_ready
    if _database_ready:
        return
    url = make_url(database_url.replace("+asyncpg", ""))
    target_db = url.database
    maintenance_url = url.set(database="postgres")
    conn = await asyncpg.connect(maintenance_url.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target_db
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target_db}"')
    finally:
        await conn.close()
    _database_ready = True


@pytest_asyncio.fixture(autouse=True)
async def _reset_settings_cache():
    """Clear the get_settings() lru_cache before each test.

    Some tests (e.g. test_config.py) rebuild Settings from monkeypatched env
    vars and never restore the cache afterwards; without this, a poisoned
    Settings instance (e.g. a fake REDIS_URL) would leak into later tests
    that call get_settings() with no monkeypatching of their own.
    """
    get_settings.cache_clear()
    yield


@pytest_asyncio.fixture(autouse=True)
async def _create_schema():
    # Import models so their tables register on Base.metadata.
    import gatekeep.models  # noqa: F401

    await _ensure_database_exists(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_ratelimit_redis_client():
    """Drop the cached rate-limit Redis client so each test (own event loop) gets a fresh one.

    Also flushes the `ratelimit:*` and `cache:exact:*` namespaces on teardown
    so leftover state from a previous test can't leak into the next one.
    This matters because `_create_schema` resets the api_keys id sequence
    every test, so a new key can collide with a stale Redis bucket/cache
    entry left by an earlier test's key of the same id.

    The flush only runs if the test actually opened a Redis connection
    (`ratelimit._redis is not None`) rather than unconditionally calling
    `get_redis()` in setup: forcing a connection for every test - including
    ones like test_config.py that monkeypatch REDIS_URL mid-test - ties
    this fixture to fixture/monkeypatch ordering across tests and caused a
    ConnectionError cascade in practice.
    """
    import gatekeep.middleware.ratelimit as ratelimit

    ratelimit._redis = None
    yield
    if ratelimit._redis is not None:
        redis = ratelimit._redis
        async for key in redis.scan_iter("ratelimit:*"):
            await redis.delete(key)
        async for key in redis.scan_iter("cache:exact:*"):
            await redis.delete(key)
        await redis.aclose()
    ratelimit._redis = None


@pytest_asyncio.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def db_ping():
    async with SessionLocal() as s:
        result = await s.execute(text("SELECT 1"))
        return result.scalar_one()
