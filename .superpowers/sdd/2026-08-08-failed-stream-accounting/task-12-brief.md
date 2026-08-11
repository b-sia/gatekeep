## Task 12: Full-suite verification and lint

**Files:** none modified - verification only.

- [ ] **Step 1: Run the full Python test suite**

Run: `source .venv/bin/activate && pytest tests/ -q`
Expected: PASS, zero failures, zero errors.

- [ ] **Step 2: Run ruff lint and format checks**

Run: `source .venv/bin/activate && ruff check gatekeep tests && ruff format --check gatekeep tests`
Expected: no findings on any file this plan touched (`gatekeep/app.py`, `gatekeep/accounting.py`, `gatekeep/models.py`, `gatekeep/observability/latency.py`, `gatekeep/api/dashboard.py`, `migrations/versions/0013_request_log_outcome.py`, `tests/test_accounting.py`, `tests/test_latency.py`, `tests/test_endpoint.py`, `tests/test_messages_endpoint.py`, `tests/test_dashboard.py`). If ruff reports pre-existing findings elsewhere in the repo unrelated to this change, leave them - only findings in touched files block this task.

Fix any findings with `ruff check --fix gatekeep tests` and `ruff format gatekeep tests`, then re-run Step 1 to confirm nothing broke.

- [ ] **Step 3: Frontend build**

Run: `cd dashboard && npm run build`
Expected: succeeds (already verified in Task 11, re-confirmed here as part of the full-suite gate in case later tasks touched shared files).

- [ ] **Step 4: Verify the migration applies cleanly (manual, if a dev Postgres is available)**

If a local `gatekeep` dev Postgres is running and reachable via `DATABASE_URL` (see the memory note on Docker/`DOCKER_HOST` for this repo's local setup): run `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head` again, confirming no errors. This exercises `migrations/versions/0013_request_log_outcome.py` outside the test suite (which builds its schema from `Base.metadata` directly, not via alembic). If no dev database is available in this environment, note that explicitly rather than skipping silently.

- [ ] **Step 5: Re-read the design spec's Testing section (§8) end to end**

Open `docs/superpowers/specs/2026-08-07-failed-stream-accounting-design.md` and check off each of its 7 numbered test scenarios against what was actually implemented:

1. Provider error mid-stream -> Task 5/6, Step 1.
2. Client disconnect mid-stream -> Task 5/6, Step 1.
3. Failure before first token -> Task 5, Step 1 (`test_client_disconnect_before_first_token_has_null_duration`).
4. Latency exclusion -> Task 8.
5. Cost inclusion -> Task 9.
6. Non-streaming provider error -> Task 7.
7. Clean stream regression -> Task 5/6, Step 5 (full existing suite re-run).

If any scenario has no corresponding test, add it before considering this plan complete.

- [ ] **Step 6: No commit for this task** (verification only - if Step 2 required fixes, those were already committed as part of fixing them; if not, nothing to commit).

---

## Out of Scope (carried over from the design spec - do not implement)

- Refactoring `gatekeep/embeddings.py` onto the shared `estimate_tokens` helper.
- A real tokenizer to replace the char-count heuristic.
- Adding an `outcome` label to any Prometheus metric.
- Capturing a distinct "time to failure" quantity separate from TTLT.
