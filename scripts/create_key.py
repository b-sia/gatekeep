"""Mint an API key for an account and print the raw key exactly once.

Creates the account if it does not exist yet. Fixes the previous breakage
where an ApiKey was inserted with no account_id (non-nullable since the
accounts migration).

Usage: python scripts/create_key.py <account-name> [key-name]
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
    if len(sys.argv) not in (2, 3):
        print(
            "usage: python scripts/create_key.py <account-name> [key-name]",
            file=sys.stderr,
        )
        raise SystemExit(1)
    account_arg = sys.argv[1]
    key_arg = sys.argv[2] if len(sys.argv) == 3 else "default"
    asyncio.run(main(account_arg, key_arg))
