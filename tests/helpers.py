from __future__ import annotations

import itertools

from gatekeep.models import Account, ApiKey
from gatekeep.providers.base import CompletionResult

_account_name_counter = itertools.count(1)


async def create_account(
    session,
    *,
    name: str | None = None,
    monthly_budget_usd: float | None = None,
    is_operator: bool = False,
) -> Account:
    """Create and flush an Account for tests, returning it with its id populated.

    Flushes (not commits) so callers can add keys in the same transaction.
    `Account.name` is globally unique, so when `name` is omitted a fresh one is
    generated (`acct-1`, `acct-2`, ...) rather than defaulting to a fixed
    string that would collide the second time a test calls this helper.

    Args:
        session: The async DB session to add the account through.
        name: Display name for the account, or None to auto-generate a unique one.
        monthly_budget_usd: Shared monthly spend cap, or None for unlimited.
        is_operator: Whether the account gets the fleet-wide dashboard view.

    Returns:
        The persisted Account with its `id` populated.
    """
    if name is None:
        name = f"acct-{next(_account_name_counter)}"
    account = Account(name=name, monthly_budget_usd=monthly_budget_usd, is_operator=is_operator)
    session.add(account)
    await session.flush()
    return account


async def create_key(
    session,
    account: Account,
    *,
    name: str = "c",
    key_hash: str = "h",
    active: bool = True,
) -> ApiKey:
    """Create and flush an ApiKey attached to `account`, returning it with its id.

    Args:
        session: The async DB session to add the key through.
        account: The Account the key belongs to.
        name: The key's display name (unique per account).
        key_hash: The stored sha256 hash of the raw key.
        active: Whether the key is active.

    Returns:
        The persisted ApiKey with its `id` populated.
    """
    key = ApiKey(
        name=name,
        key_hash=key_hash,
        account_id=account.id,
        active=active,
    )
    session.add(key)
    await session.flush()
    return key


class FakeProvider:
    """Provider stub returning queued texts (or raising queued exceptions) in order,
    one per complete() call."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.payloads = []

    async def complete(self, payload):
        self.payloads.append(payload)
        text = self._texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return CompletionResult(text=text, input_tokens=1, output_tokens=1, stop_reason="stop")
