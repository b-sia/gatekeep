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
    await engine.dispose()


@pytest_asyncio.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def db_ping():
    async with SessionLocal() as s:
        result = await s.execute(text("SELECT 1"))
        return result.scalar_one()
