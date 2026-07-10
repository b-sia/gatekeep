import pytest_asyncio
from sqlalchemy import text

from gatekeep.config import get_settings
from gatekeep.db import Base, SessionLocal, engine


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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_ratelimit_redis_client():
    """Drop the cached rate-limit Redis client so each test (own event loop) gets a fresh one."""
    import gatekeep.middleware.ratelimit as ratelimit

    ratelimit._redis = None
    yield
    if ratelimit._redis is not None:
        await ratelimit._redis.aclose()
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
