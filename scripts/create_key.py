"""Insert an API key and print the raw key exactly once.

Usage: python scripts/create_key.py "my client name"
"""

import asyncio
import sys

from gatekeep.auth_keys import generate_key, hash_key
from gatekeep.db import SessionLocal
from gatekeep.models import ApiKey


async def main(name: str) -> None:
    raw = generate_key()
    async with SessionLocal() as session:
        session.add(ApiKey(name=name, key_hash=hash_key(raw)))
        await session.commit()
    print(raw)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('usage: python scripts/create_key.py "client name"', file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main(sys.argv[1]))
