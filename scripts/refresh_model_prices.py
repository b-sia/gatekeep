#!/usr/bin/env python3
"""Regenerate the vendored model pricing baseline from BerriAI/litellm.

Downloads LiteLLM's community pricing dataset, transforms the chat models on
providers we bill into our compact schema (see gatekeep.pricing.transform_litellm),
and rewrites gatekeep/data/model_prices.json - preserving every hand-maintained
``source == "local"`` entry (preview/self-hosted models no public dataset
knows) and replacing the ``source == "litellm"`` entries with fresh values.

Run manually or via the weekly ``refresh-model-prices`` GitHub Action, which
opens a PR with the diff for a human to review before it lands - staleness
becomes a reviewed PR instead of a silent gap in the enforcement path.

Usage:
    python scripts/refresh_model_prices.py [--check]

    --check  Exit non-zero (without writing) if the file would change. Used in
             CI to detect drift.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# Make the gatekeep package importable when run as a plain script from the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gatekeep.pricing import transform_litellm  # noqa: E402

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
VENDORED_PATH = Path(__file__).resolve().parent.parent / "gatekeep" / "data" / "model_prices.json"


def fetch_litellm(url: str = LITELLM_URL, *, timeout: int = 30) -> dict:
    """Download and parse the LiteLLM pricing dataset."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted URL)
        return json.loads(resp.read().decode())


def build_models(existing: dict, litellm_raw: dict) -> dict:
    """Merge fresh LiteLLM entries over the existing file's `local` entries.

    Local entries always survive and win over a LiteLLM entry with the same
    key, so a hand-maintained preview price is never clobbered by the dataset.
    """
    local = {
        key: value
        for key, value in existing.get("models", {}).items()
        if value.get("source") == "local"
    }
    merged = dict(transform_litellm(litellm_raw))
    merged.update(local)  # local wins on key collision
    return dict(sorted(merged.items()))


def render(existing: dict, models: dict) -> str:
    """Render the full vendored file as pretty JSON, refreshing the meta stamp."""
    meta = dict(existing.get("_meta", {}))
    meta["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload = {"_meta": meta, "models": models}
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def main() -> int:
    """Refresh (or, with --check, verify) the vendored pricing file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the file would change; do not write.",
    )
    args = parser.parse_args()

    existing = json.loads(VENDORED_PATH.read_text())
    litellm_raw = fetch_litellm()
    models = build_models(existing, litellm_raw)

    # --check ignores the generated_at stamp (which always differs) and compares
    # only the model entries, since that is the meaningful drift.
    if args.check:
        if models != existing.get("models", {}):
            print(
                "model_prices.json is stale; run scripts/refresh_model_prices.py",
                file=sys.stderr,
            )
            return 1
        print("model_prices.json is up to date.")
        return 0

    VENDORED_PATH.write_text(render(existing, models))
    litellm_count = sum(1 for v in models.values() if v.get("source") == "litellm")
    local_count = sum(1 for v in models.values() if v.get("source") == "local")
    print(
        f"Wrote {len(models)} models "
        f"({litellm_count} litellm, {local_count} local) to {VENDORED_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
