## Task 10: Dashboard success-rate stat (backend)

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `UsageSummaryResponse.failed_count: int` and `UsageSummaryResponse.success_rate: float`, returned by `GET /dashboard/api/usage/summary`.
- Consumed by: Task 11 (frontend).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_dashboard.py`:

```python
async def test_usage_summary_includes_failed_count_and_success_rate(
    client, raw_key, session
):
    key_id = await _key_id(session, raw_key)
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome="ok")
    await _seed_log(session, key_id=key_id, model="gpt-4o", outcome=None)
    await _seed_log(
        session, key_id=key_id, model="gpt-4o", outcome="provider_error"
    )
    await _seed_log(
        session, key_id=key_id, model="gpt-4o", outcome="client_disconnect"
    )

    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()

    assert body["request_count"] == 4
    assert body["failed_count"] == 2
    assert body["success_rate"] == pytest.approx(0.5)


async def test_usage_summary_success_rate_is_zero_for_an_empty_window(
    client, raw_key
):
    r = await client.get(
        "/dashboard/api/usage/summary",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    body = r.json()
    assert body["request_count"] == 0
    assert body["failed_count"] == 0
    assert body["success_rate"] == 0.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v -k "failed_count or success_rate"`
Expected: FAIL with a pydantic validation / `KeyError`-shaped failure (`failed_count`/`success_rate` not in the response body).

- [ ] **Step 3: Extend `UsageSummaryResponse` and `usage_summary` in `gatekeep/api/dashboard.py`**

Add fields to `UsageSummaryResponse`, after `cache_hit_rate`:

```python
class UsageSummaryResponse(BaseModel):
    """Aggregate cost/usage totals over a time range, plus breakdowns by
    model, API key, and prompt name."""

    start: datetime
    end: datetime
    request_count: int
    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    spend_usd: float
    savings_usd: float
    cache_hit_count: int
    cache_hit_rate: float
    failed_count: int
    success_rate: float
    by_model: list[UsageBreakdownRow]
    by_key: list[UsageBreakdownRow]
    by_prompt: list[UsageBreakdownRow]
```

Add a failed-row count to the totals query in `usage_summary`. Replace the `totals_row` block:

```python
    _FAILED_OUTCOMES = ("provider_error", "client_disconnect")

    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, 0.0), else_=RequestLog.cost_usd)
                    ),
                    0.0,
                ),
                func.coalesce(
                    func.sum(
                        case((RequestLog.cached, RequestLog.cost_usd), else_=0.0)
                    ),
                    0.0,
                ),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (RequestLog.outcome.in_(_FAILED_OUTCOMES), 1), else_=0
                        )
                    ),
                    0,
                ),
            ).where(*filters)
        )
    ).one()
    (
        request_count,
        total_tokens,
        prompt_tokens,
        completion_tokens,
        cost_usd,
        spend_usd,
        savings_usd,
        cache_hit_count,
        failed_count,
    ) = totals_row
    request_count = int(request_count)
    cache_hit_count = int(cache_hit_count)
    failed_count = int(failed_count)
    cache_hit_rate = (cache_hit_count / request_count) if request_count else 0.0
    success_rate = (
        (request_count - failed_count) / request_count if request_count else 0.0
    )
```

`_FAILED_OUTCOMES` is placed as a module-level constant instead (matching `_NO_PROMPT_LABEL`'s placement style), not inline inside the function - move it up near `_NO_PROMPT_LABEL`:

```python
_NO_PROMPT_LABEL = "(none)"
_FAILED_OUTCOMES = ("provider_error", "client_disconnect")
```

(and remove the inline `_FAILED_OUTCOMES = (...)` line added above inside the function body).

Add `failed_count=failed_count, success_rate=success_rate,` to the `UsageSummaryResponse(...)` construction at the end of `usage_summary`, after `cache_hit_rate=cache_hit_rate,`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v -k "failed_count or success_rate"`
Expected: PASS

- [ ] **Step 5: Run the full dashboard test file to check for regressions**

Run: `source .venv/bin/activate && pytest tests/test_dashboard.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): add failed_count/success_rate to usage summary"
```

---

