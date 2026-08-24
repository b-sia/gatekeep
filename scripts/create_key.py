"""Mint an API key for an account and print the raw key exactly once.

Creates the account if it does not exist yet. Fixes the previous breakage
where an ApiKey was inserted with no account_id (non-nullable since the
accounts migration).

Both positional arguments are required: the old single-arg call shape
(`create_key.py "client name"`) used to name the *key*, not an account.
Requiring both args here makes any leftover old-shape call fail loudly
with argparse's usage message instead of silently minting a "default"
key under a new account named after what used to be the key name.

Pass `--operator` to make the account a fleet-wide operator. This is the
bootstrap path for the first operator on a fresh database: the dashboard's
prompt/eval/account routes all require operator access, and there is no
in-product way to promote the very first account (a chicken-and-egg
problem), so it has to be granted out-of-band here.

Usage: python scripts/create_key.py [--operator] <account-name> <key-name>
"""

import argparse
import asyncio

from sqlalchemy import select

from gatekeep import account_service
from gatekeep.db import SessionLocal
from gatekeep.models import Account


async def main(account_name: str, key_name: str, *, operator: bool = False) -> None:
    """Ensure `account_name` exists, mint `key_name` on it, and print the raw key.

    Args:
        account_name: Account to mint under; created if it does not exist.
        key_name: Name for the new key on that account.
        operator: When True, ensure the account has operator access - set at
            creation for a new account, or promoted via `set_operator` for an
            existing one. When False, an existing account's flag is untouched.
    """
    async with SessionLocal() as session:
        account = (
            await session.execute(select(Account).where(Account.name == account_name))
        ).scalar_one_or_none()
        if account is None:
            account = await account_service.create_account(
                session, name=account_name, is_operator=operator
            )
        elif operator and not account.is_operator:
            await account_service.set_operator(session, account.id, True)
        _key, raw = await account_service.create_key(session, account.id, key_name)
    print(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("account_name", help="account to mint under (created if absent)")
    parser.add_argument("key_name", help="name for the new key on that account")
    parser.add_argument(
        "--operator",
        action="store_true",
        help="grant the account fleet-wide operator access (bootstraps the first operator)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.account_name, args.key_name, operator=args.operator))
