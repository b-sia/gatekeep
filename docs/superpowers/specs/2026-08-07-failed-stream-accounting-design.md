# Reliable accounting for failed and aborted streams

Design spec for issue #17: "Failed or aborted streams write no RequestLog row,
losing token accounting and budget spend."

## Problem

When a streamed completion fails partway through, Gatekeep writes no
`RequestLog` row at all. In both SSE generators (`_messages_sse`, `_sse` in
`gatekeep/app.py`), `timer.finish()` / `log_request` / `observe_request` live
inside the `elif isinstance(ev, StreamEnd)` branch, which only runs on a clean
provider completion. Every other exit path skips all three:

1. **Provider raises mid-stream** - the `except Exception` handler yields an
   in-band error event and falls through to the closing event. No accounting.
2. **Client disconnects mid-stream** - the generator receives `GeneratorExit` /
   `asyncio.CancelledError`, neither an `Exception` subclass, so nothing runs.
   No accounting, no error event.

Consequences:

- **Billing.** A stream that emits thousands of tokens then dies bills upstream
  but records `$0`. `budget.record_spend` never runs, so the key's monthly cap
  does not decrement and repeated failures can exceed `monthly_budget_usd`.
- **Observability drift.** `model`/`path` are marked before the
  `StreamingResponse` returns, so `LatencyMiddleware` still observes
  `request_duration_seconds{path="stream"}`, but `state["provider_ms"]` is never
  published, so the middleware takes the `_UNSET` skip branch and omits
  `gateway_overhead_seconds`. Prometheus counts a request that has no
  `request_logs` row and no overhead observation.

The non-streaming path has a milder version: `gatekeep/app.py:510-513` returns
`map_provider_error(exc)` without logging or publishing `provider_ms`.

## Key constraint discovered during design

The provider `stream()` abstraction surfaces token counts **only on
`StreamEnd`**; `TextDelta` carries text only (`gatekeep/providers/base.py`). On
a mid-stream failure `StreamEnd` never arrives, so the generator has **no
authoritative token counts** - neither input nor output. Any failed-row
accounting must therefore *estimate*. The codebase has no real tokenizer; it
already uses a documented "~4 characters per token" heuristic
(`gatekeep/embeddings.py:8`).

## Decisions

- **Token/cost on failure: estimate from accumulated text.** Reuse the ~4-char/
  token heuristic. Records real (approximate) spend so the budget counter
  decrements. Self-contained in the generators - no provider-layer change.
- **Scope:** both SSE generators + the non-streaming provider-error path + a
  nullable `outcome` column + latency-aggregate exclusion + a dashboard
  success-rate stat.
- **Failed-stream TTLT is computed accurately, from the last emitted token, not
  the failure moment** (see below), stored in the DB, and kept out of the
  unlabeled Prometheus TTLT histogram.

## Design

### 1. Token/cost estimation on failure

Add `estimate_tokens(text: str) -> int` centralizing the ~4-chars/token
heuristic currently inlined only in `embeddings.py`. Home it where both the
generators and (future) callers can reach it (e.g. `gatekeep/accounting.py`
alongside `calculate_cost`, or a small `gatekeep/tokens.py`); `embeddings.py`
may later be refactored onto it but that is not required here.

Each generator accumulates delta text as it is yielded. At failure:

- `output_tokens = estimate_tokens(accumulated_delta_text)`
- `input_tokens  = estimate_tokens(<concatenated payload message text>)`
- `cost = calculate_cost(model, input_tokens, output_tokens)`

The estimated cost feeds both `log_request` (so `record_spend` decrements the
budget) and `observe_request` (so the Prometheus token/cost histograms stay
consistent with the request count). These values are explicitly approximate;
the `outcome` column lets any consumer distinguish estimated failed rows from
authoritative clean ones. On a clean `StreamEnd` the provider's authoritative
counts are used, unchanged.

### 2. `outcome` column + migration

New nullable column on `request_logs`:

```
outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
```

Values: `ok` / `provider_error` / `client_disconnect`. Nullable so pre-migration
rows stay `NULL` (legacy, treated as `ok`-equivalent by latency filters). New
migration `0013_request_log_outcome.py`, following the shape of
`0012_request_log_path.py`. This slots beside `path` exactly as the
observability-consolidation spec anticipated - no reshape of the table.

`log_request` gains `outcome: str = "ok"`, written onto the row.

### 3. Generator restructure (`_sse` and `_messages_sse`)

Move `timer.finish()` / `log_request` / `observe_request` out of the
`StreamEnd` branch so they run on every exit path. Shape:

```
outcome = "ok"; input_tokens = output_tokens = 0; stop_reason = None
accumulated: list[str] = []
try:
    timer.provider_started()
    async for ev in provider.stream(payload):
        if TextDelta:
            timer.delta(); accumulated.append(ev.text); yield <chunk>
        elif StreamEnd:
            outcome = "ok"
            input_tokens, output_tokens = ev.input_tokens, ev.output_tokens
            stop_reason = ev.stop_reason
            yield <closing chunks>          # content_block_stop/message_delta or final_chunk
except Exception as exc:
    outcome = "provider_error"
    input_tokens  = estimate_tokens(<payload text>)
    output_tokens = estimate_tokens("".join(accumulated))
    yield <in-band error event>
except (GeneratorExit, asyncio.CancelledError):
    outcome = "client_disconnect"
    input_tokens  = estimate_tokens(<payload text>)
    output_tokens = estimate_tokens("".join(accumulated))
    raise                                    # re-raise after setting state
finally:
    # NEVER yield here - illegal during GeneratorExit
    timings = timer.finish(succeeded=(outcome == "ok"))
    async with SessionLocal() as session:
        await log_request(..., outcome=outcome,
                          prompt_tokens=input_tokens, completion_tokens=output_tokens,
                          duration_ms=timings.duration_ms, provider_ms=timings.provider_ms,
                          ttft_ms=timings.ttft_ms)
    observe_request(model=model, prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    cost_usd=calculate_cost(model, input_tokens, output_tokens))
# closing message_stop / [DONE] stays OUTSIDE finally, on the non-cancelled paths only
yield <message_stop or [DONE]>
```

Constraints the implementation must honour:

- **`finally` never yields.** Yielding during `GeneratorExit` raises
  `RuntimeError`. Accounting only (DB + metrics) goes in `finally`.
- **Closing events stay off the cancellation path.** The trailing
  `message_stop` / `[DONE]` is emitted after the `try/except/finally` and is
  unreachable when the generator was cancelled (the `raise` re-propagates before
  it). A disconnected client cannot receive an error event either - matching
  existing behaviour; the fix records the row, it does not resurrect the
  connection.
- **`finally` accounting runs during cancellation.** It uses its own
  `SessionLocal()` (as today). Awaiting a commit while a `CancelledError` is in
  flight is the delicate part; the implementation must ensure the DB write and
  metric observation complete before the exception propagates (e.g. run the
  accounting under `asyncio.shield`, or otherwise structure it so the awaited
  commit is not itself cancelled). Covered explicitly by tests.

### 4. StreamTimer: accurate failed-stream TTLT

TTLT for a failed stream is well-defined and computed **server-side** - the
gateway already stamps `StreamTimer._last_delta_at` at the moment it emits each
delta into the response body. There is no client-received timestamp available
(SSE is one-way; the client sends no back-signal), and note `_last_delta_at`
includes downstream backpressure, as `StreamTimer` already documents.

The failure moment and the last-token moment are different timestamps, so each
latency quantity uses its own reference point:

| Quantity                | Reference on failure | Rationale |
|-------------------------|----------------------|-----------|
| `duration_ms` (= TTLT)  | `_last_delta_at`     | Time to the last token actually generated; excludes any dead wait/hang before the failure. `NULL` if no token ever arrived. |
| `provider_ms`           | failure moment (`now`) | Real upstream time spent, including the failed wait; keeps middleware overhead (`e2e - provider_ms`) correct. Published to `state` so `gateway_overhead_seconds` is still recorded. |
| `ttft_ms`               | first-delta time (already recorded) | Unchanged. |

`StreamTimer.finish()` gains a `succeeded: bool` parameter (default preserving
current behaviour):

- **Clean (`succeeded=True`):** unchanged. `duration_ms = now - started_at`
  (`now` ~ last token, since `StreamEnd` immediately follows the last delta),
  observes `time_to_last_token_seconds`, publishes `provider_ms`.
- **Failed (`succeeded=False`):** `duration_ms = (_last_delta_at - started_at)`
  if a delta arrived else `None`; `provider_ms = now - provider_started_at`
  published to `state`; **does not** observe `time_to_last_token_seconds` (see
  §5); `ttft_ms` is whatever was recorded (may be `None`).

`ttft_seconds` and `inter_token_seconds` are observed live per delta and so
inherently retain partial data for the deltas that did arrive on a failed
stream - this is expected and documented, not a bug to fix.

### 5. Aggregate / metric semantics

- **Cost / spend** (`budget.get_period_spend`, dashboard cost sums): unchanged -
  failed rows are non-cached and already included. The money was spent, so it
  counts.
- **DB latency percentiles** (`_latency_filters`, `gatekeep/api/dashboard.py`):
  add `(RequestLog.outcome.is_(None)) | (RequestLog.outcome == "ok")` so failed
  rows drop out of `duration_ms` / `provider_ms` / overhead percentiles. The
  accurate failed-row `duration_ms` remains stored and available for
  outcome-aware queries.
- **Prometheus `time_to_last_token_seconds`:** success-only. Failed streams are
  not observed into it (no `outcome` label exists to separate them later), which
  matches the DB percentile exclusion. No metric-schema change.
- **Cache hit rate:** untouched - streaming bypasses the cache entirely.

### 6. Non-streaming provider-error path

At `gatekeep/app.py:510-513`, when `provider.complete` raises: before returning
`map_provider_error(exc)`, publish `provider_ms` via `observe_non_streaming`
(resolving the middleware overhead skip) and write
`log_request(outcome="provider_error")` with zero tokens / zero cost. No partial
output exists on this path, so estimation does not apply - this is the milder,
"money usually not spent" case the issue calls out, handled with the same
`outcome` mechanism for consistency.

### 7. Dashboard success-rate stat

- **Backend:** extend the usage aggregate to expose, per window, total request
  count and failed count (grouped by `outcome`, where `outcome IN
  ('provider_error','client_disconnect')` counts as failed and `ok`/`NULL`
  counts as success), plus a derived `success_rate`.
- **Frontend:** one stat tile / derived stat in the analytics view showing
  success rate (and failed count). Kept minimal; no new page or chart.

### 8. Testing (reproduction-first, TDD)

Per the issue's reproduction sketch and the global "reproduce E2E first" rule:

1. **Provider error mid-stream.** A provider stub whose `stream()` yields
   several `TextDelta`s then raises. `POST` with `stream: true`. Assert: SSE body
   carries the deltas plus an error event; a `request_logs` row exists with
   `outcome='provider_error'`, non-zero estimated tokens/cost, `duration_ms` =
   last-token time (not failure time); budget counter decremented;
   `gateway_overhead_seconds` observed.
2. **Client disconnect mid-stream.** Drive the generator to receive
   `GeneratorExit` / `asyncio.CancelledError` after some deltas. Assert: a
   `request_logs` row exists with `outcome='client_disconnect'`, estimated
   tokens/cost, accurate `duration_ms`, budget decremented, overhead observed;
   no error event required.
3. **Failure before first token.** `duration_ms`/`ttft_ms` are `NULL`; row still
   written with the right `outcome`.
4. **Latency exclusion.** A failed row does not move `duration_ms` percentiles in
   the dashboard latency query, while a clean row does.
5. **Cost inclusion.** A failed row's estimated cost is included in
   `get_period_spend` and dashboard cost sums.
6. **Non-streaming provider error.** A `provider_error` row is written with zero
   tokens; `provider_ms` published so overhead is observed.
7. **Clean stream regression.** Existing clean-stream accounting is unchanged
   (authoritative tokens, `outcome='ok'`, TTLT/Prometheus observed as before).

## Out of scope

- Refactoring `embeddings.py` onto the shared `estimate_tokens` helper (may
  follow, not required here).
- A real tokenizer to replace the char-count heuristic.
- Adding an `outcome` label to any Prometheus metric.
- Capturing a distinct "time to failure" quantity separate from TTLT.
