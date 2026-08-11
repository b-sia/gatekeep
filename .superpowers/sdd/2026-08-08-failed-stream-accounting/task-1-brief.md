## Task 1: `estimate_tokens` helper and `log_request` `outcome` param

**Files:**
- Modify: `gatekeep/accounting.py`
- Test: `tests/test_accounting.py` (new file - no existing accounting-only test file exists; check `tests/` first in case one appeared)

**Interfaces:**
- Produces: `estimate_tokens(text: str) -> int` in `gatekeep.accounting`, importable by `gatekeep/app.py`.
- Produces: `log_request(..., outcome: str = "ok", ...)` - `outcome` is written onto `RequestLog.outcome` (added in Task 2). Until Task 2 lands, this task cannot commit the `outcome=...` write to the DB - see Step ordering below, which lands the column first via a fast-follow inside this same task rather than splitting Task 1/2 apart, since `log_request` cannot be meaningfully tested writing a column that doesn't exist yet.

- [ ] **Step 1: Check for an existing accounting test file**

Run: `ls tests/ | grep -i account`

If `tests/test_accounting.py` already exists, read it and add to it instead of creating a new file with a colliding name.

- [ ] **Step 2: Write the failing test for `estimate_tokens`**

Create `tests/test_accounting.py`:

```python
from __future__ import annotations

import pytest

from gatekeep.accounting import estimate_tokens


def test_estimate_tokens_empty_string_is_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_rounds_up_to_at_least_one_token():
    assert estimate_tokens("hi") == 1


def test_estimate_tokens_matches_four_chars_per_token_on_exact_multiples():
    assert estimate_tokens("a" * 8) == 2


def test_estimate_tokens_rounds_up_on_a_partial_final_token():
    assert estimate_tokens("a" * 9) == 3
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `source .venv/bin/activate && pytest tests/test_accounting.py -v`
Expected: FAIL with `ImportError: cannot import name 'estimate_tokens'`

- [ ] **Step 4: Implement `estimate_tokens` in `gatekeep/accounting.py`**

Add near the top of `gatekeep/accounting.py`, after the `MODEL_PRICING` dict and before `calculate_cost`:

```python
def estimate_tokens(text: str) -> int:
    """Estimate a token count for `text` using the ~4-characters-per-token
    heuristic, matching the proxy limit `gatekeep.embeddings` already uses
    for the same reason: this codebase has no real tokenizer.

    Used only where an authoritative provider-reported token count is
    unavailable - a mid-stream provider error or client disconnect never
    reaches `StreamEnd`, so the failed row's tokens/cost are approximate.

    Rounds up so any non-empty text counts as at least one token; empty
    text is zero tokens.

    Args:
        text: The text to estimate a token count for.

    Returns:
        The estimated token count, always >= 0, and >= 1 for any non-empty text.
    """
    if not text:
        return 0
    return -(-len(text) // 4)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `source .venv/bin/activate && pytest tests/test_accounting.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add gatekeep/accounting.py tests/test_accounting.py
git commit -m "feat(accounting): add estimate_tokens heuristic for failed-stream cost accounting"
```

---

