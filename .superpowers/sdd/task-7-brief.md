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

