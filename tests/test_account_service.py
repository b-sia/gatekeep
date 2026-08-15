from __future__ import annotations

import pytest

from gatekeep import account_service as svc
from gatekeep.models import Account
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
