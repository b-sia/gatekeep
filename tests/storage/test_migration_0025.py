import pytest
from sqlalchemy import inspect

from gatekeep.storage.db import engine


@pytest.mark.asyncio
async def test_signup_tables_and_status_column_exist():
    async with engine.connect() as conn:
        names = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert {"account_credentials", "sessions", "email_tokens"} <= set(names)
        cols = await conn.run_sync(
            lambda c: [col["name"] for col in inspect(c).get_columns("accounts")]
        )
        assert "status" in cols
