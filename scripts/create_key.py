"""Mint an API key for an account and print the raw key exactly once.

Creates the account if it does not exist yet. Fixes the previous breakage
where an ApiKey was inserted with no account_id (non-nullable since the
accounts migration).

Both arguments are required: the old single-arg call shape
(`create_key.py "client name"`) used to name the *key*, not an account.
Requiring both args here makes any leftover old-shape call fail loudly
with the usage message below instead of silently minting a "default"
key under a new account named after what used to be the key name.

Usage: python scripts/create_key.py <account-name> <key-name>
"""

import asyncio
import sys

from sqlalchemy import select

from gatekeep import account_service
from gatekeep.db import SessionLocal
from gatekeep.models import Account


async def main(account_name: str, key_name: str) -> None:
    """Ensure `account_name` exists, mint `key_name` on it, and print the raw key."""
    async with SessionLocal() as session:
        account = (
            await session.execute(select(Account).where(Account.name == account_name))
        ).scalar_one_or_none()
        if account is None:
            account = await account_service.create_account(session, name=account_name)
        _key, raw = await account_service.create_key(session, account.id, key_name)
    print(raw)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(
            "usage: python scripts/create_key.py <account-name> <key-name>",
            file=sys.stderr,
        )
        raise SystemExit(1)
    account_arg = sys.argv[1]
    key_arg = sys.argv[2]
    asyncio.run(main(account_arg, key_arg))
