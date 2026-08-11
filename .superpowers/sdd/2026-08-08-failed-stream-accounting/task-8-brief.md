## Task 8: Dashboard latency-percentile exclusion of failed rows

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `RequestLog.outcome` (Task 2).
- Produces: `_latency_filters` now also excludes `outcome NOT IN ('provider_error', 'client_disconnect')` rows - every latency endpoint (`latency_summary`, `latency_timeseries`) picks this up automatically since they all call `_latency_filters`.

- [ ] **Step 1: Extend the `_seed_log` test helper to accept `outcome`**

In `tests/test_dashboard.py`, update `_seed_log`'s signature and body to add an `outcome` parameter:

```python
async def _seed_log(
    session,
    *,
    key_id: int,
    model: str,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    cost_usd: float = 0.01,
    cached: bool = False,
    prompt_name: str | None = None,
    created_at: datetime | None = None,
    path: str | None = None,
    duration_ms: float | None = None,
    provider_ms: float | None = None,
    ttft_ms: float | None = None,
    outcome: str | None = None,
) -> RequestLog:
    """Insert one RequestLog row directly, optionally backdating created_at.

    `path`/`duration_ms`/`provider_ms`/`ttft_ms` default to None, matching a
    pre-0012 row: such rows are deliberately excluded from every latency
    query, so latency tests must pass `path` explicitly. `outcome` defaults
    to None, matching a pre-0013 (or successful) row.
    """
    log = RequestLog(
        key_id=key_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        cost_usd=cost_usd,
        cached=cached,
        response_id=f"resp-{uuid4()}",
        prompt_name=prompt_name,
        path=path,
        duration_ms=duration_ms,
        provider_ms=provider_ms,
        ttft_ms=ttft_ms,
        outcome=outcome,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    if created_at is not None:
        await session.execute(
            update(RequestLog)
            .where(RequestLog.id == log.id)
            .values(created_at=created_at)
        )
        await session.commit()
    return log
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_dashboard.py`, near the other latency tests (after `test_latency_overhead_excludes_uncached_row_with_no_provider_ms`):

```python
async def test_latency_summary_excludes_failed_outcome_rows(client, raw_key, session):
    """A failed row's accurate duration_ms is still stored (see the design
    spec), but must not move the percentiles - only outcome='ok'/NULL rows
    are latency-eligible."""
    key_id = await _key_id(session, raw_key)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=100.0,
        provider_ms=80.0,
        outcome=None,
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=9999.0,
        provider_ms=9999.0,
        outcome="provider_error",
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="stream",
        duration_ms=9999.0,
        provider_ms=9999.0,
        outcome="client_disconnect",
    )

    r = await client.get(
        "/dashboard/api/latency/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["sample_count"] == 1
    assert body["stream_ttlt_ms"]["p50_ms"] == 100.0


async def test_latency_timeseries_excludes_failed_outcome_rows(client, raw_key, session):
    key_id = await _key_id(session, raw_key)
    now = datetime.now(timezone.utc)
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=100.0,
        provider_ms=80.0,
        created_at=now,
        outcome="ok",
    )
    await _seed_log(
        session,
        key_id=key_id,
        model="gpt-4o",
        path="provider",
        duration_ms=9999.0,
        provider_ms=9999.0,
        created_at=now,
        outcome="provider_error",
    )

    r = await client.get(
        "/dashboard/api/latency/timeseries",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert len(body["buckets"]) == 1
    assert body["buckets"][0]["sample_count"] == 1
    assert body["buckets"][0]["e2e_p50_ms"] == 100.0
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v -k excludes_failed_outcome`
Expected: FAIL - `sample_count` is 3 (or timeseries bucket includes both rows) since the filter doesn't exist yet.

- [ ] **Step 4: Update `_latency_filters` in `gatekeep/api/dashboard.py`**

Add `or_` to the sqlalchemy import at the top of the file:

```python
from sqlalchemy import Integer, case, func, or_, select, true
```

Replace `_latency_filters`:

```python
def _latency_filters(
    start: datetime,
    end: datetime,
    *,
    model: str | None,
    key_id: int | None,
    prompt_name: str | None,
) -> list:
    """Build the WHERE clauses for a latency query: the usual usage filters
    plus the latency-eligibility conditions.

    `path IS NOT NULL` excludes rows written between migrations 0011 and
    0012, which carry timings but no path - nothing after the fact can tell
    a streamed one from a non-streamed one, so they cannot be assigned to
    either side of the streaming split. This self-heals as those rows age
    out of the reporting window.

    The `outcome` condition excludes failed rows (`provider_error` /
    `client_disconnect`, #17): their `duration_ms` is real (see
    StreamTimer.finish(succeeded=False)), but a percentile blending "how
    long a normal request takes" with "how long a request took before it
    failed" would describe neither quantity. NULL passes through (a
    pre-0013 row, or any row logged without an explicit outcome), matching
    how NULL `path` predates migration 0012.
    """
    return [
        *_base_filters(
            start, end, model=model, key_id=key_id, prompt_name=prompt_name
        ),
        RequestLog.path.isnot(None),
        RequestLog.duration_ms.isnot(None),
        or_(
            RequestLog.outcome.is_(None),
            RequestLog.outcome == "ok",
        ),
    ]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v -k excludes_failed_outcome`
Expected: PASS

- [ ] **Step 6: Run the full dashboard test file to check for regressions**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v`
Expected: PASS, no regressions (existing `_seed_log` calls all default `outcome=None`, which passes the new `or_` condition).

- [ ] **Step 7: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "fix(dashboard): exclude failed-outcome rows from latency percentiles"
```

---

