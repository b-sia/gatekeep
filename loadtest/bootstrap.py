"""Mint load-testing API keys via the real account/key service and write
them to loadtest/keys.json (git-ignored) for locustfile.py to read.

Reuses gatekeep.accounts.account_service (not raw DB inserts) so keys exist
exactly as they would in production - see
docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §5.

Safe to re-run: every run mints a fresh set of accounts (name-suffixed with
the current timestamp) rather than reusing/colliding with a prior run's.

Usage: python loadtest/bootstrap.py [--pool-size N] [--budget-keys N] [--budget-usd AMOUNT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from gatekeep.accounts import account_service
from gatekeep.storage.db import SessionLocal

_KEYS_PATH = Path(__file__).parent / "keys.json"


async def _mint_pool_keys(run_id: int, n: int) -> list[str]:
    """Mint `n` API keys on unlimited-budget accounts for the
    throughput/latency/breaking-point scenarios.

    Rate limiting is one process-wide value already raised well above target
    load by the compose override (loadtest/docker-compose.loadtest.yml), so
    pool size need not be sized against a per-key rate limit - see the
    design doc §5.

    Args:
        run_id: Timestamp suffix shared by all accounts minted in this run,
            keeping account names unique across re-runs.
        n: Number of accounts/keys to mint.

    Returns:
        The raw keys, one per minted account, in creation order.

    Raises:
        AccountNameConflictError: if a `loadtest-pool-{run_id}-{i}` name is
            already taken (only possible if the script is re-run within the
            same second as a prior run).
        KeyNameConflictError: if the account already has a key named
            `"loadtest"` (unreachable for a freshly created account, but
            propagated uncaught from `account_service.create_key`).
    """
    raw_keys: list[str] = []
    async with SessionLocal() as session:
        for i in range(n):
            account = await account_service.create_account(
                session, name=f"loadtest-pool-{run_id}-{i}"
            )
            _key, raw = await account_service.create_key(session, account.id, "loadtest")
            raw_keys.append(raw)
    return raw_keys


async def _mint_budget_keys(run_id: int, n: int, budget_usd: float) -> list[str]:
    """Mint `n` API keys on dedicated low-budget accounts for the budget half
    of the enforcement scenario (design doc §6.4).

    Args:
        run_id: Timestamp suffix shared by all accounts minted in this run,
            keeping account names unique across re-runs.
        n: Number of accounts/keys to mint.
        budget_usd: Monthly spend cap set on each account.

    Returns:
        The raw keys, one per minted account, in creation order.

    Raises:
        AccountNameConflictError: if a `loadtest-budget-{run_id}-{i}` name is
            already taken (only possible if the script is re-run within the
            same second as a prior run).
        InvalidBudgetError: if `budget_usd` is not positive.
        KeyNameConflictError: if the account already has a key named
            `"loadtest"` (unreachable for a freshly created account, but
            propagated uncaught from `account_service.create_key`).
    """
    raw_keys: list[str] = []
    async with SessionLocal() as session:
        for i in range(n):
            account = await account_service.create_account(
                session, name=f"loadtest-budget-{run_id}-{i}", monthly_budget_usd=budget_usd
            )
            _key, raw = await account_service.create_key(session, account.id, "loadtest")
            raw_keys.append(raw)
    return raw_keys


async def main(pool_size: int, budget_keys: int, budget_usd: float) -> None:
    """Mint both key pools and write them to keys.json.

    Args:
        pool_size: Number of unlimited-budget accounts/keys to mint.
        budget_keys: Number of low-budget accounts/keys to mint.
        budget_usd: Monthly spend cap set on each budget account.

    Raises:
        AccountNameConflictError: propagated from `_mint_pool_keys`/
            `_mint_budget_keys` if a name collides (shouldn't normally
            happen given the run_id suffix, but is possible if the script
            is re-run within the same second as a prior run).
        InvalidBudgetError: propagated from `_mint_budget_keys` if
            `budget_usd` is not positive.
        KeyNameConflictError: propagated from `_mint_pool_keys`/
            `_mint_budget_keys` (extremely unlikely here since key names are
            fixed as `"loadtest"` per account, but still reachable).
    """
    run_id = int(time.time())
    pool = await _mint_pool_keys(run_id, pool_size)
    budget = await _mint_budget_keys(run_id, budget_keys, budget_usd)
    _KEYS_PATH.write_text(json.dumps({"pool": pool, "budget": budget}, indent=2))
    print(f"wrote {len(pool)} pool keys and {len(budget)} budget keys to {_KEYS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pool-size", type=int, default=20)
    parser.add_argument("--budget-keys", type=int, default=3)
    parser.add_argument("--budget-usd", type=float, default=1.0)
    args = parser.parse_args()
    asyncio.run(main(args.pool_size, args.budget_keys, args.budget_usd))
