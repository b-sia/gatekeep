## Task 9: Cost-inclusion regression check for failed rows

**Files:**
- Modify: `tests/test_dashboard.py`

**Interfaces:** none new - this task is pure verification that Task 8's filter change didn't accidentally touch cost aggregation, which the design spec explicitly requires stay untouched (failed rows' estimated cost still counts, "the money was spent").

- [ ] **Step 1: Write the test**

Add to `tests/test_dashboard.py`, near the usage-summary tests:

```python
async def test_usage_summary_includes_cost_of_failed_rows(client, raw_key, session):
    """Cost/spend aggregates are unchanged by #17: a failed row's estimated
    cost still counts (the money was spent), unlike the latency percentiles
    Task 8 excludes it from."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session, key_id=key_id, model="gpt-4o", cost_usd=0.5, outcome="ok"
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        cost_usd=0.25,
        outcome="provider_error",
    )

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["request_count"] == 2
    assert body["cost_usd"] == pytest.approx(0.75)
    assert body["spend_usd"] == pytest.approx(0.75)
```

- [ ] **Step 2: Run the test**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v -k includes_cost_of_failed_rows`
Expected: PASS immediately (no production code change needed - `usage_summary`'s filters/aggregates never reference `outcome`). This is a regression-pinning test, not new behavior; if it fails, something in this plan's earlier tasks leaked an `outcome` filter into `_base_filters` or the cost aggregate, which would be a bug to find and fix before proceeding.

- [ ] **Step 3: Commit**

```bash
git add tests/test_dashboard.py
git commit -m "test(dashboard): pin cost aggregates as unaffected by failed-row outcome tagging"
```

---

