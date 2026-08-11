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

