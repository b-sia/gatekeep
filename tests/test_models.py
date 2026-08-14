import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.models import Account, ApiKey
from tests.helpers import create_account


def test_generate_and_hash_are_stable():
    raw = generate_key()
    assert raw.startswith("gk-")
    assert hash_key(raw) == hash_key(raw)
    assert hash_key(raw) != hash_key(generate_key())


async def test_api_key_persists(session):
    raw = generate_key()
    account = await create_account(session)
    session.add(ApiKey(name="test", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()

    found = (
        await session.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    ).scalar_one()
    assert found.name == "test"
    assert found.active is True
    assert found.created_at is not None
    assert found.account_id == account.id


async def test_account_owns_keys_and_name_unique_per_account(session):
    """ApiKey.name is unique per (account_id, name), not globally."""
    acct_a = Account(name="team-a")
    acct_b = Account(name="team-b")
    session.add_all([acct_a, acct_b])
    await session.flush()

    # Same key name is allowed under two different accounts.
    session.add(ApiKey(name="prod", key_hash="h1", account_id=acct_a.id))
    session.add(ApiKey(name="prod", key_hash="h2", account_id=acct_b.id))
    await session.commit()

    # Duplicate name within one account is rejected.
    session.add(ApiKey(name="prod", key_hash="h3", account_id=acct_a.id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_account_defaults(session):
    """A freshly created account defaults to non-operator, unlimited budget."""
    acct = Account(name="team-c")
    session.add(acct)
    await session.commit()
    assert acct.is_operator is False
    assert acct.monthly_budget_usd is None


async def test_account_name_is_globally_unique(session):
    """Account.name has a global uniqueness constraint, unlike ApiKey.name."""
    session.add(Account(name="only-one"))
    await session.commit()

    session.add(Account(name="only-one"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
