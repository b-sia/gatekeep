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
