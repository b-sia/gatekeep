## Task 4: `_run_shielded` cancellation-safe accounting helper

**Files:**
- Modify: `gatekeep/app.py`
- Test: `tests/test_endpoint.py`

**Interfaces:**
- Produces: `_run_shielded(coro: Coroutine) -> Any` (module-private in `gatekeep.app`). Task 5 uses it to wrap the `finally`-block accounting write in both SSE generators.

This is isolated first because it's pure `asyncio` mechanics with no DB/HTTP dependency, so it can be TDD'd fast and in isolation before it's wired into the generators in Task 5.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_endpoint.py`, near the top after the imports (add `import asyncio` to the import block):

```python
async def test_run_shielded_completes_the_coroutine_despite_repeated_cancellation():
    """A disconnecting client can inject cancellation into the SSE generator's
    `finally` block more than once (e.g. a persistent cancel scope). The
    accounting write there must run to completion regardless.

    `_run_shielded` absorbs the outer cancellations rather than letting them
    cut the DB commit short - it does NOT re-raise CancelledError to its
    caller for an outer cancellation (only if the wrapped coroutine's own
    task is itself done/cancelled, which never happens here). In the real
    generator, the CancelledError the client disconnect caused is already
    propagating via the `except ... raise` clause that ran before `finally`;
    this helper's job is only to keep the write from being cut short while
    that propagation is paused, not to re-signal the cancellation itself.
    So `runner()` below completes normally even though its task was
    cancelled twice - that is the correct, intended behavior."""
    completed = False

    async def slow_write():
        nonlocal completed
        await asyncio.sleep(0.05)
        completed = True

    async def runner():
        await app_module._run_shielded(slow_write())

    task = asyncio.ensure_future(runner())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # cancel again while the shielded write is still in flight
    await task  # must NOT raise: both cancellations are absorbed until the write finishes
    assert completed, "the shielded write must run to completion despite repeated cancellation"


async def test_run_shielded_returns_the_coroutines_result_when_not_cancelled():
    async def compute():
        return 42

    result = await app_module._run_shielded(compute())
    assert result == 42
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py -v -k run_shielded`
Expected: FAIL with `AttributeError: module 'gatekeep.app' has no attribute '_run_shielded'`

- [ ] **Step 3: Implement `_run_shielded` in `gatekeep/app.py`**

Add `import asyncio` to the top of `gatekeep/app.py` (alongside the existing `import json`, `import logging`, `import pathlib`, `import time`).

Add this helper near `_anthropic_event`/`_event` (after `_anthropic_event`, before `_sse`):

```python
async def _run_shielded(coro):
    """Await `coro` to completion, even if the calling task is cancelled one
    or more times while doing so.

    Used for the streaming generators' failure-path accounting: their
    `finally` block runs during `GeneratorExit`/`asyncio.CancelledError`
    (a client disconnecting mid-stream), and a real disconnect can inject
    cancellation more than once - e.g. a persistent cancel scope keeps
    re-raising it at every `await` until the scope itself exits. A bare
    `await coro` there would let a second cancellation cut the DB commit
    short and drop the row. `asyncio.shield` protects the wrapped task from
    the *outer* cancellation, but the outer `await` still raises
    CancelledError immediately when cancelled - so this loops, re-awaiting
    the same underlying task, until that task has actually finished.

    Args:
        coro: The coroutine to run to completion.

    Returns:
        Whatever `coro` returns.

    Raises:
        asyncio.CancelledError: only once the wrapped task itself is done
            (either with a result or, in principle, its own cancellation -
            which nothing here ever triggers).
        BaseException: whatever `coro` itself raises.
    """
    task = asyncio.ensure_future(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                raise
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_endpoint.py -v -k run_shielded`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add gatekeep/app.py tests/test_endpoint.py
git commit -m "feat(app): add _run_shielded helper for cancellation-safe accounting writes"
```

---

