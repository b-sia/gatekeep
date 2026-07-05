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
