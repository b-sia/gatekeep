from __future__ import annotations

import pytest

from gatekeep.accounts import account_service as svc
from gatekeep.accounts.auth_keys import hash_key as _hash_key
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.storage.models import Account, ApiKey, RequestLog
from tests.helpers import create_account


async def test_create_account_persists_and_returns_it(session):
    """create_account inserts the row, commits, and returns it with an id."""
    account = await svc.create_account(session, name="acme", monthly_budget_usd=10.0)
    assert account.id is not None
    assert account.name == "acme"
    assert account.monthly_budget_usd == 10.0
    assert account.is_operator is False
    fetched = await session.get(Account, account.id)
    assert fetched is not None


async def test_create_account_rejects_duplicate_name(session):
    """A globally-unique name collision raises AccountNameConflictError."""
    await svc.create_account(session, name="dup")
    with pytest.raises(svc.AccountNameConflictError):
        await svc.create_account(session, name="dup")


async def test_create_account_rejects_non_positive_budget(session):
    """A zero or negative budget raises InvalidBudgetError."""
    with pytest.raises(svc.InvalidBudgetError):
        await svc.create_account(session, name="cheap", monthly_budget_usd=0)


async def test_create_account_rejects_blank_name(session):
    """A blank or whitespace-only name raises InvalidAccountNameError."""
    with pytest.raises(svc.InvalidAccountNameError):
        await svc.create_account(session, name="   ")


async def test_rename_account_changes_name(session):
    """rename_account updates the name and returns the account."""
    account = await create_account(session)
    await session.commit()
    renamed = await svc.rename_account(session, account.id, "renamed")
    assert renamed.name == "renamed"


async def test_rename_missing_account_raises(session):
    """rename_account on an unknown id raises AccountNotFoundError."""
    with pytest.raises(svc.AccountNotFoundError):
        await svc.rename_account(session, 999999, "whatever")


async def test_rename_account_rejects_blank_name(session):
    """Renaming to a blank or whitespace-only name raises InvalidAccountNameError."""
    account = await create_account(session)
    await session.commit()
    with pytest.raises(svc.InvalidAccountNameError):
        await svc.rename_account(session, account.id, "   ")


async def test_rename_to_taken_name_conflicts(session):
    """Renaming onto an existing name raises AccountNameConflictError."""
    a = await create_account(session, name="a")
    await create_account(session, name="b")
    await session.commit()
    with pytest.raises(svc.AccountNameConflictError):
        await svc.rename_account(session, a.id, "b")


async def test_set_budget_sets_and_clears(session):
    """set_budget stores a positive amount and clears it with None."""
    account = await create_account(session)
    await session.commit()
    await svc.set_budget(session, account.id, 25.0)
    assert (await session.get(Account, account.id)).monthly_budget_usd == 25.0
    await svc.set_budget(session, account.id, None)
    assert (await session.get(Account, account.id)).monthly_budget_usd is None


async def test_set_budget_rejects_non_positive(session):
    """set_budget with a non-positive amount raises InvalidBudgetError."""
    account = await create_account(session)
    await session.commit()
    with pytest.raises(svc.InvalidBudgetError):
        await svc.set_budget(session, account.id, -1.0)


async def test_set_operator_toggles(session):
    """set_operator flips the flag on when at least one other operator remains."""
    keeper = await create_account(session, name="keeper", is_operator=True)
    target = await create_account(session, name="target")
    await session.commit()
    await svc.set_operator(session, target.id, True)
    assert (await session.get(Account, target.id)).is_operator is True
    # keeper referenced so the fixture's operator is unambiguous
    assert keeper.is_operator is True


async def test_set_operator_last_operator_guard(session):
    """Turning off the only operator raises LastOperatorError."""
    only = await create_account(session, name="only", is_operator=True)
    await session.commit()
    with pytest.raises(svc.LastOperatorError):
        await svc.set_operator(session, only.id, False)


async def test_set_operator_off_allowed_with_another_operator(session):
    """Turning off one operator is allowed when another operator exists."""
    a = await create_account(session, name="op-a", is_operator=True)
    await create_account(session, name="op-b", is_operator=True)
    await session.commit()
    off = await svc.set_operator(session, a.id, False)
    assert off.is_operator is False


async def test_create_key_returns_raw_and_stores_hash(session):
    """create_key returns the raw key once and persists only its sha256 hash."""
    account = await create_account(session)
    await session.commit()
    key, raw = await svc.create_key(session, account.id, "prod")
    assert raw.startswith("gk-")
    assert key.name == "prod"
    assert key.active is True
    assert key.account_id == account.id
    assert key.key_hash == _hash_key(raw)


async def test_create_key_duplicate_name_conflicts(session):
    """A duplicate key name within the same account raises KeyNameConflictError."""
    account = await create_account(session)
    await session.commit()
    await svc.create_key(session, account.id, "dup")
    with pytest.raises(svc.KeyNameConflictError):
        await svc.create_key(session, account.id, "dup")


async def test_create_key_missing_account_raises(session):
    """Minting for an unknown account raises AccountNotFoundError."""
    with pytest.raises(svc.AccountNotFoundError):
        await svc.create_key(session, 999999, "x")


async def test_list_keys_returns_account_keys(session):
    """list_keys returns exactly the target account's keys."""
    a = await create_account(session, name="a")
    b = await create_account(session, name="b")
    await session.commit()
    await svc.create_key(session, a.id, "a1")
    await svc.create_key(session, b.id, "b1")
    keys = await svc.list_keys(session, a.id)
    assert [k.name for k in keys] == ["a1"]


async def test_revoke_key_soft_revokes(session):
    """revoke_key flips active to False and leaves the row in place."""
    account = await create_account(session)
    await session.commit()
    key, _ = await svc.create_key(session, account.id, "prod")
    revoked = await svc.revoke_key(session, account.id, key.id)
    assert revoked.active is False
    assert await session.get(ApiKey, key.id) is not None


async def test_revoke_key_other_account_raises(session):
    """Revoking a key that belongs to another account raises KeyNotFoundError."""
    a = await create_account(session, name="a")
    b = await create_account(session, name="b")
    await session.commit()
    key, _ = await svc.create_key(session, b.id, "b1")
    with pytest.raises(svc.KeyNotFoundError):
        await svc.revoke_key(session, a.id, key.id)


async def test_list_accounts_with_stats_counts_and_spend(session):
    """Stats include active/total key counts and non-cached MTD spend."""
    redis = get_redis()
    await redis.flushdb()
    account = await create_account(session, name="acme", monthly_budget_usd=100.0)
    await session.commit()
    active, _ = await svc.create_key(session, account.id, "k-active")
    revoked, _ = await svc.create_key(session, account.id, "k-revoked")
    await svc.revoke_key(session, account.id, revoked.id)
    session.add(
        RequestLog(
            key_id=active.id,
            account_id=account.id,
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=3.0,
            cached=False,
            response_id="r1",
        )
    )
    await session.commit()

    stats = await svc.list_accounts_with_stats(session, redis)
    row = next(s for s in stats if s.name == "acme")
    assert row.active_key_count == 1
    assert row.total_key_count == 2
    assert row.spend_mtd == pytest.approx(3.0)
    assert row.monthly_budget_usd == 100.0
