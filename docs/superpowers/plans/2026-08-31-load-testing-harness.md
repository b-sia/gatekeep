# Load-Testing Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give gatekeep a repeatable way to load-test its own request-handling
overhead (auth, rate limiting, budgets, caching, cost accounting, routing,
the per-request Postgres write) in isolation from any real upstream
provider's latency or cost.

**Architecture:** An in-app, flag-gated `StubProvider` returns canned,
zero-cost responses with latency/size/inter-token-delay encoded in the model
string (`stub/lat50-out200`). It is wired into the existing provider
registry exactly like the other four providers, but only when
`loadtest_stub_enabled` is true. A `docker-compose.loadtest.yml` override
enables the flag and raises the process-wide rate limit out of the way, a
`bootstrap.py` script mints real API keys through the existing account
service, and a Locust `locustfile.py` drives four scenarios (throughput,
latency SLO, breaking point, budget/rate-limit enforcement) against the
running stack, read via the existing Prometheus/Grafana setup.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy async, Redis, pytest,
Locust (new dependency, `loadtest` extra), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-08-30-load-testing-harness-design.md`

## Global Constraints

- `loadtest_stub_enabled` defaults to `False` and must never be enabled in production.
- `gatekeep/routing/pricing.py` (`BILLED_PROVIDERS`, `is_billed_provider`, `is_unpriced`) does **not** change - `stub` is deliberately kept out of it.
- Stub billing is a single fixed per-1M-token USD price (`STUB_PRICE_PER_1M`) applied to both input and output tokens, defined in `gatekeep/accounts/accounting.py`, not looked up in the pricing table.
- `_providers` in `gatekeep/app.py` is built once at import time from the module-level `_settings = get_settings()`; stub registration must be a plain `if` guard at that same import-time scope, never deferred/lazy logic.
- `resolve_route`'s `Literal` return type gains `"stub"` everywhere it appears (3 call sites: `gatekeep/api/translation.py` x2, `gatekeep/api/anthropic_translation.py` x1).
- No per-account/per-key rate-limit override (out of scope for v1 - rate limiting stays one process-wide value).
- No CI-gated performance thresholds (future work).
- Locust scenarios must reuse a small, fixed, documented set of stub model strings (cardinality guardrail on `model`-labeled Prometheus histograms) - never generate parameterizations dynamically.
- Keep the full `pytest` suite and `ruff check` / `ruff format --check` green after every task.
- Never use an em dash in code, comments, docs, or commit messages - use a plain dash.

---

## File Structure

| File | Responsibility |
|---|---|
| `gatekeep/providers/stub.py` | New. `StubProvider` + model-string parsing (`parse_stub_model`), canned-text generation. |
| `gatekeep/config.py` | Add `loadtest_stub_enabled: bool = False`. |
| `.env.example` | Document the new flag. |
| `gatekeep/api/translation.py` | `resolve_route` gains a `stub/` branch; `Literal` widens. |
| `gatekeep/api/anthropic_translation.py` | `messages_to_payload`'s `Literal` widens to match. |
| `gatekeep/app.py` | `_GatewayProvider` widens; provider construction extracted into a testable `_build_providers(settings)`, gated on the flag. |
| `gatekeep/accounts/accounting.py` | `STUB_PRICE_PER_1M` constant + a `calculate_cost` branch for `provider == "stub"`. |
| `loadtest/docker-compose.loadtest.yml` | New. Compose override: enables the flag, raises rate limits. |
| `loadtest/bootstrap.py` | New. Mints pool + low-budget keys via `account_service`, writes `loadtest/keys.json`. |
| `loadtest/locustfile.py` | New. Four `HttpUser` scenarios + two `LoadTestShape` ramps. |
| `loadtest/README.md` | New. Runbook + results template. |
| `loadtest/results/.gitkeep` | New. Keeps the (gitignored-contents) results dir tracked. |
| `pyproject.toml` | Add `loadtest` optional-dependency group (`locust`). |
| `.gitignore` | Ignore `loadtest/keys.json` and `loadtest/results/*.csv`/`*.html`. |
| `justfile` | `loadtest-up` / `loadtest-bootstrap` / `loadtest <scenario>` / `loadtest-down`. |

Tests live beside the existing suite: `tests/providers/test_stub_provider.py`,
`tests/api/test_translation.py` (extended), `tests/providers/test_get_provider.py`
(extended), `tests/accounts/test_accounting.py` (extended), `tests/test_config.py`
(extended), and two new integration files
`tests/test_loadtest_stub_smoke.py` / `tests/test_loadtest_budget_enforcement.py`.

---

### Task 1: Config flag - `loadtest_stub_enabled`

**Files:**
- Modify: `gatekeep/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.loadtest_stub_enabled: bool` (default `False`), read by later tasks via `get_settings()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_loadtest_stub_enabled_defaults_to_false():
    s = Settings(database_url="x", redis_url="y", anthropic_api_key="z")
    assert s.loadtest_stub_enabled is False


def test_loadtest_stub_enabled_reads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    monkeypatch.setenv("REDIS_URL", "redis://h:6379/0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    s = get_settings()
    assert s.loadtest_stub_enabled is True
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'loadtest_stub_enabled'`

- [ ] **Step 3: Implement**

In `gatekeep/config.py`, add after `email_token_ttl_seconds: int = 86_400` (end of the class body):

```python
    # --- Load testing ---
    # Registers the in-app stub provider (gatekeep/providers/stub.py) under
    # the "stub" name and lets `stub/...` models resolve to it, so a
    # load-testing harness can exercise the gateway's own overhead (auth,
    # rate limiting, budget checks, caching, cost accounting, routing)
    # without calling a real upstream. Never enable this in production - see
    # docs/superpowers/specs/2026-08-30-load-testing-harness-design.md.
    loadtest_stub_enabled: bool = False
```

In `.env.example`, append a new section at the end of the file:

```
# --- Load testing (see loadtest/README.md) ---
# Registers the in-app stub provider and stub/ model routing. Never set this
# to true outside a local load-testing run.
LOADTEST_STUB_ENABLED=false
```

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gatekeep/config.py .env.example tests/test_config.py
git commit -m "feat(loadtest): add loadtest_stub_enabled config flag"
```

---

### Task 2: `StubProvider` model-string parsing

**Files:**
- Create: `gatekeep/providers/stub.py`
- Test: `tests/providers/test_stub_provider.py`

**Interfaces:**
- Consumes: nothing (pure parsing logic).
- Produces: `StubParams` dataclass (`latency_ms: float`, `output_tokens: int`, `itl_ms: float`), `parse_stub_model(model: str) -> StubParams`, `DEFAULT_LATENCY_MS = 100.0`, `DEFAULT_OUTPUT_TOKENS = 100`. Consumed by Task 3's `StubProvider` and referenced by `loadtest/locustfile.py` (Task 11) as the model-string contract.

`parse_stub_model` receives the model id **after** `resolve_route` has already
stripped the `stub/` prefix (mirroring how `openai/`/`google/` prefixes are
stripped in `gatekeep/api/translation.py`) - so its input looks like
`"lat50-out200"`, `"default"`, or `""`, never `"stub/lat50-out200"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/test_stub_provider.py`:

```python
from __future__ import annotations

import pytest

from gatekeep.providers.stub import (
    DEFAULT_LATENCY_MS,
    DEFAULT_OUTPUT_TOKENS,
    StubParams,
    parse_stub_model,
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("default", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("garbage", StubParams(DEFAULT_LATENCY_MS, DEFAULT_OUTPUT_TOKENS, 1.0)),
        ("lat50-out200", StubParams(50.0, 200, 0.25)),
        ("lat50-out200-itl5", StubParams(50.0, 200, 5.0)),
        # unknown/malformed segments are ignored, not fatal
        ("lat50-bogus-out200", StubParams(50.0, 200, 0.25)),
        ("out0", StubParams(DEFAULT_LATENCY_MS, 0, 0.0)),
    ],
)
def test_parse_stub_model(model, expected):
    assert parse_stub_model(model) == expected
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/providers/test_stub_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'gatekeep.providers.stub'`

- [ ] **Step 3: Implement**

Create `gatekeep/providers/stub.py`:

```python
from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_LATENCY_MS = 100.0
DEFAULT_OUTPUT_TOKENS = 100

_SEGMENT_RE = re.compile(r"^(lat|out|itl)(\d+)$")


@dataclass(frozen=True)
class StubParams:
    """Parsed load-test parameters encoded in a stub model string.

    Attributes:
        latency_ms: Delay before the first (and, for a non-streaming call,
            only) response - drives time-to-first-token in streaming.
        output_tokens: Number of canned output tokens to generate.
        itl_ms: Delay between successive streamed text deltas.
    """

    latency_ms: float
    output_tokens: int
    itl_ms: float


def parse_stub_model(model: str) -> StubParams:
    """Parse a stub model id (already stripped of its `stub/` prefix by
    `resolve_route`) into `StubParams`.

    Recognizes hyphen-separated `lat<ms>`, `out<tokens>`, `itl<ms>` segments
    in any order, e.g. `"lat50-out200-itl5"`. Parsing is total and forgiving:
    `""`, `"default"`, and any unparseable or partially-parseable suffix
    (unknown segments are skipped, not fatal) fall back to documented
    defaults rather than raising, so a load script never fails on a typo
    mid-run.

    When `itl_ms` is not given, it defaults to `latency_ms / output_tokens`
    (0 if `output_tokens` is 0) - scenarios with a bigger configured latency
    also get a slower per-token cadence by default, without needing to spell
    out all three segments.

    Args:
        model: The stub model id, with any `stub/` prefix already removed.

    Returns:
        The parsed (or defaulted) `StubParams`.
    """
    latency_ms: float | None = None
    output_tokens: int | None = None
    itl_ms: float | None = None
    if model and model != "default":
        for segment in model.split("-"):
            match = _SEGMENT_RE.match(segment)
            if match is None:
                continue
            kind, raw_value = match.group(1), int(match.group(2))
            if kind == "lat":
                latency_ms = float(raw_value)
            elif kind == "out":
                output_tokens = raw_value
            elif kind == "itl":
                itl_ms = float(raw_value)
    latency_ms = DEFAULT_LATENCY_MS if latency_ms is None else latency_ms
    output_tokens = DEFAULT_OUTPUT_TOKENS if output_tokens is None else output_tokens
    if itl_ms is None:
        itl_ms = latency_ms / output_tokens if output_tokens else 0.0
    return StubParams(latency_ms=latency_ms, output_tokens=output_tokens, itl_ms=itl_ms)
```

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/providers/test_stub_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gatekeep/providers/stub.py tests/providers/test_stub_provider.py
git commit -m "feat(loadtest): add stub model-string parsing"
```

---

### Task 3: `StubProvider.complete()` / `.stream()`

**Files:**
- Modify: `gatekeep/providers/stub.py`
- Modify: `tests/providers/test_stub_provider.py`

**Interfaces:**
- Consumes: `parse_stub_model`, `StubParams` (Task 2); `estimate_tokens` from `gatekeep.accounts.accounting` (existing).
- Produces: `StubProvider` class implementing the provider protocol (`complete(payload) -> CompletionResult`, `stream(payload) -> AsyncIterator[TextDelta | StreamEnd]`), consumed by Task 5 (`gatekeep/app.py`'s `_build_providers`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_stub_provider.py`:

```python
import pytest

from gatekeep.providers.base import StreamEnd, TextDelta
from gatekeep.providers.stub import StubProvider


class _RecordingSleep:
    """Fake async sleep that records requested durations instead of waiting."""

    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _payload(model: str, content: str = "hi") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": content}]}


async def test_complete_sleeps_for_latency_then_returns_sized_result():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    result = await provider.complete(_payload("lat50-out20"))
    assert sleep.calls == [0.05]
    assert result.output_tokens == 20
    assert result.stop_reason == "stop"
    assert len(result.text) > 0


async def test_complete_estimates_input_tokens_from_payload():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    short = await provider.complete(_payload("default", content="hi"))
    long = await provider.complete(_payload("default", content="hi " * 100))
    assert long.input_tokens > short.input_tokens


async def test_complete_canned_text_deterministic_for_same_size():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    first = await provider.complete(_payload("out50"))
    second = await provider.complete(_payload("out50"))
    assert first.text == second.text


async def test_stream_ttft_then_itl_between_deltas_then_streamend():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    events = [event async for event in provider.stream(_payload("lat50-out4-itl5"))]
    *deltas, end = events
    assert all(isinstance(d, TextDelta) for d in deltas)
    assert len(deltas) == 4
    assert isinstance(end, StreamEnd)
    assert end.stop_reason == "stop"
    assert end.output_tokens == 4
    # TTFT sleep, then 3 inter-token sleeps between the 4 deltas (none after
    # the last delta - StreamEnd follows immediately).
    assert sleep.calls == [0.05, 0.005, 0.005, 0.005]


async def test_stream_zero_output_tokens_yields_only_streamend():
    sleep = _RecordingSleep()
    provider = StubProvider(sleep=sleep)
    events = [event async for event in provider.stream(_payload("out0"))]
    assert len(events) == 1
    assert isinstance(events[0], StreamEnd)
    assert events[0].output_tokens == 0
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/providers/test_stub_provider.py -v`
Expected: FAIL with `ImportError: cannot import name 'StubProvider'`

- [ ] **Step 3: Implement**

Append to `gatekeep/providers/stub.py` (add these imports at the top alongside
the existing ones, and the new code below the existing `parse_stub_model`):

```python
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from gatekeep.accounts.accounting import estimate_tokens
from gatekeep.providers.base import CompletionResult, StreamEnd, TextDelta
```

```python
def _canned_text(output_tokens: int) -> str:
    """Deterministic canned text sized to roughly 4 characters per token,
    matching the heuristic `accounting.estimate_tokens` uses elsewhere."""
    if output_tokens <= 0:
        return ""
    word = "gatekeep-stub "
    target_chars = output_tokens * 4
    repeated = (word * (target_chars // len(word) + 1))[:target_chars]
    return repeated.strip()


def _chunk_text(text: str, n: int) -> list[str]:
    """Split `text` into at most `n` roughly equal, order-preserving pieces."""
    if n <= 0 or not text:
        return []
    size = max(1, -(-len(text) // n))
    return [text[i : i + size] for i in range(0, len(text), size)][:n]


def _payload_text(payload: dict[str, Any]) -> str:
    """Concatenate a payload's system prompt and message contents, for
    estimating a plausible input-token count."""
    parts = [payload.get("system") or ""]
    parts.extend(m.get("content", "") for m in payload.get("messages", []))
    return "\n".join(parts)


class StubProvider:
    """Zero-cost, deterministic provider for load testing gatekeep's own
    request-handling overhead. Never calls a real upstream - every
    parameter (latency, output size, inter-token delay) is decoded from the
    model string by `parse_stub_model`. See
    docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §1.
    """

    def __init__(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """Args:
            sleep: Injected async sleep, real `asyncio.sleep` by default.
                Tests pass a fake to assert durations without waiting.
        """
        self._sleep = sleep

    async def complete(self, payload: dict[str, Any]) -> CompletionResult:
        """Sleep for the parsed latency, then return a canned, sized result."""
        params = parse_stub_model(payload["model"])
        await self._sleep(params.latency_ms / 1000)
        return CompletionResult(
            text=_canned_text(params.output_tokens),
            input_tokens=estimate_tokens(_payload_text(payload)),
            output_tokens=params.output_tokens,
            stop_reason="stop",
        )

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[TextDelta | StreamEnd]:
        """Sleep once for TTFT, then yield sized deltas spaced by the parsed
        inter-token delay, followed by a terminal `StreamEnd`."""
        params = parse_stub_model(payload["model"])
        await self._sleep(params.latency_ms / 1000)
        chunks = _chunk_text(_canned_text(params.output_tokens), params.output_tokens)
        for i, chunk in enumerate(chunks):
            yield TextDelta(text=chunk)
            if i < len(chunks) - 1:
                await self._sleep(params.itl_ms / 1000)
        yield StreamEnd(
            stop_reason="stop",
            input_tokens=estimate_tokens(_payload_text(payload)),
            output_tokens=params.output_tokens,
        )
```

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/providers/test_stub_provider.py -v`
Expected: PASS (all parsing + provider tests)

- [ ] **Step 5: Commit**

```bash
git add gatekeep/providers/stub.py tests/providers/test_stub_provider.py
git commit -m "feat(loadtest): implement StubProvider complete/stream"
```

---

### Task 4: Routing - `stub/` prefix in `resolve_route`

**Files:**
- Modify: `gatekeep/api/translation.py`
- Modify: `gatekeep/api/anthropic_translation.py`
- Modify: `tests/api/test_translation.py`

**Interfaces:**
- Produces: `resolve_route(requested, *, aliases) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], str]`, consumed by Task 5's endpoint wiring (already calls `get_provider(provider_name)` generically - no other app.py changes needed here).

- [ ] **Step 1: Write the failing test**

In `tests/api/test_translation.py`, add a case to the existing `test_resolve_route`
parametrize list (right after the `google/` case):

```python
        ("google/gemini-flash-latest", ("google", "gemini-flash-latest")),
        ("stub/lat50-out200", ("stub", "lat50-out200")),
    ],
)
def test_resolve_route(model, expected):
```

(This replaces the existing 2-line tail of the list plus the `def` line with
the same lines plus the new case - the rest of the parametrize block is
unchanged.)

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/api/test_translation.py::test_resolve_route -v`
Expected: FAIL - `stub/lat50-out200` currently falls through to `("ollama", "stub/lat50-out200")`, not `("stub", "lat50-out200")`.

- [ ] **Step 3: Implement**

In `gatekeep/api/translation.py`, update `resolve_route`:

```python
def resolve_route(
    requested: str, *, aliases: dict[str, str]
) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], str]:
    """Determine which provider should serve `requested` and its resolved model id.

    A `openai/`, `google/`, or `stub/` prefix routes directly to that
    provider with the prefix stripped (e.g. "openai/gpt-4o" ->
    ("openai", "gpt-4o")), bypassing the alias table entirely - this is how a
    client opts into the real upstream (or the load-test stub) instead of the
    alias table's Claude substitution. Otherwise checks the alias table,
    passes through anything already prefixed `claude-` as Anthropic, and
    otherwise routes to Ollama with the model name unchanged (assumed to be a
    local Ollama tag).
    """
    if requested.startswith("openai/"):
        return "openai", requested.removeprefix("openai/")
    if requested.startswith("google/"):
        return "google", requested.removeprefix("google/")
    if requested.startswith("stub/"):
        return "stub", requested.removeprefix("stub/")
    if requested in aliases:
        return "anthropic", aliases[requested]
    if requested.startswith("claude-"):
        return "anthropic", requested
    return "ollama", requested
```

And update `openai_to_payload`'s return type annotation two lines below:

```python
def openai_to_payload(
    req: ChatCompletionRequest,
    *,
    default_max_tokens: int,
    model_aliases: dict[str, str],
) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], dict[str, Any]]:
```

In `gatekeep/api/anthropic_translation.py`, update `messages_to_payload`'s
return type annotation the same way:

```python
def messages_to_payload(
    req: MessagesRequest, *, model_aliases: dict[str, str]
) -> tuple[Literal["anthropic", "ollama", "openai", "google", "stub"], dict[str, Any]]:
```

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/api/test_translation.py tests/api/test_anthropic_translation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gatekeep/api/translation.py gatekeep/api/anthropic_translation.py tests/api/test_translation.py
git commit -m "feat(loadtest): route stub/ prefix to the stub provider"
```

---

### Task 5: App wiring - gated stub provider registration

**Files:**
- Modify: `gatekeep/app.py`
- Modify: `tests/providers/test_get_provider.py`
- Create: `tests/test_loadtest_stub_smoke.py`

**Interfaces:**
- Consumes: `StubProvider` (Task 3), `resolve_route` returning `"stub"` (Task 4), `Settings.loadtest_stub_enabled` (Task 1).
- Produces: `_build_providers(settings: Settings) -> dict[str, _GatewayProvider]` (module-level function in `gatekeep/app.py`, called once at import time as `_providers = _build_providers(_settings)`); widened `_GatewayProvider` type alias.

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_get_provider.py`:

```python
from gatekeep.app import _build_providers
from gatekeep.config import Settings
from gatekeep.providers.stub import StubProvider


def _settings(**overrides):
    return Settings(database_url="x", redis_url="y", anthropic_api_key="z", **overrides)


def test_build_providers_excludes_stub_by_default():
    providers = _build_providers(_settings())
    assert "stub" not in providers


def test_build_providers_includes_stub_when_flag_enabled():
    providers = _build_providers(_settings(loadtest_stub_enabled=True))
    assert isinstance(providers["stub"], StubProvider)
```

Create `tests/test_loadtest_stub_smoke.py`:

```python
from __future__ import annotations

import pytest

import gatekeep.app as app_module
from gatekeep.config import get_settings
from gatekeep.providers.stub import StubProvider


async def test_stub_request_returns_200_with_well_formed_body_when_registered(
    client, raw_key, monkeypatch
):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setitem(app_module._providers, "stub", StubProvider())

    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"model": "stub/lat10-out10", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"]
    assert body["usage"]["completion_tokens"] == 10


async def test_stub_request_is_inert_when_not_registered(client, raw_key):
    assert "stub" not in app_module._providers
    with pytest.raises(KeyError):
        await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw_key}"},
            json={"model": "stub/lat10-out10", "messages": [{"role": "user", "content": "ping"}]},
        )
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/providers/test_get_provider.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_providers' from 'gatekeep.app'`

Run: `pytest tests/test_loadtest_stub_smoke.py -v`
Expected: these two pass already (they only touch `app_module._providers`
directly, which already exists) - they exist here as regression coverage
for Step 3's refactor, not as TDD-red tests. Confirm they still pass after
Step 3.

- [ ] **Step 3: Implement**

In `gatekeep/app.py`, add to the imports:

```python
from gatekeep.config import Settings, get_settings
```

(replacing the existing `from gatekeep.config import get_settings` line), and:

```python
from gatekeep.providers.stub import StubProvider
```

(alongside the other `gatekeep.providers.*` imports).

Replace the provider-registry block:

```python
_settings = get_settings()
_GatewayProvider = AnthropicProvider | OllamaProvider | OpenAIProvider | GoogleProvider

_providers: dict[str, _GatewayProvider] = {
    "anthropic": AnthropicProvider(AsyncAnthropic(api_key=_settings.anthropic_api_key)),
    "ollama": OllamaProvider(ollama.AsyncClient(host=_settings.ollama_host)),
    # api_key falls back to a placeholder string (never None) so the SDK
    # client doesn't raise at import time when the key is unset - failures
    # surface as an upstream error on the first actual request instead, via
    # map_provider_error. See Settings.openai_api_key/google_api_key.
    "openai": OpenAIProvider(AsyncOpenAI(api_key=_settings.openai_api_key or "unset")),
    "google": GoogleProvider(genai.Client(api_key=_settings.google_api_key or "unset")),
}
```

with:

```python
_settings = get_settings()
_GatewayProvider = (
    AnthropicProvider | OllamaProvider | OpenAIProvider | GoogleProvider | StubProvider
)


def _build_providers(settings: Settings) -> dict[str, _GatewayProvider]:
    """Build the provider registry for one process.

    The stub provider (gatekeep/providers/stub.py) is registered under
    "stub" only when `settings.loadtest_stub_enabled` is true. When false, a
    `stub/...` request's `get_provider("stub")` call misses with a
    KeyError - the stub is inert in production regardless of the `stub/`
    routing prefix `resolve_route` always understands. See
    docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §2.
    """
    providers: dict[str, _GatewayProvider] = {
        "anthropic": AnthropicProvider(AsyncAnthropic(api_key=settings.anthropic_api_key)),
        "ollama": OllamaProvider(ollama.AsyncClient(host=settings.ollama_host)),
        # api_key falls back to a placeholder string (never None) so the SDK
        # client doesn't raise at import time when the key is unset -
        # failures surface as an upstream error on the first actual request
        # instead, via map_provider_error. See
        # Settings.openai_api_key/google_api_key.
        "openai": OpenAIProvider(AsyncOpenAI(api_key=settings.openai_api_key or "unset")),
        "google": GoogleProvider(genai.Client(api_key=settings.google_api_key or "unset")),
    }
    if settings.loadtest_stub_enabled:
        providers["stub"] = StubProvider()
    return providers


_providers: dict[str, _GatewayProvider] = _build_providers(_settings)
```

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/providers/test_get_provider.py tests/test_loadtest_stub_smoke.py tests/api -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gatekeep/app.py tests/providers/test_get_provider.py tests/test_loadtest_stub_smoke.py
git commit -m "feat(loadtest): gate stub provider registration on loadtest_stub_enabled"
```

---

### Task 6: Stub billing

**Files:**
- Modify: `gatekeep/accounts/accounting.py`
- Modify: `tests/accounts/test_accounting.py`

**Interfaces:**
- Produces: `STUB_PRICE_PER_1M: float` (module constant in `gatekeep/accounts/accounting.py`), consumed by Task 7's budget-enforcement test (via `monkeypatch.setattr`).
- Consumes: `Settings.loadtest_stub_enabled` (Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `tests/accounts/test_accounting.py`:

```python
from gatekeep.observability.metrics import unpriced_model_total


@pytest.fixture
def stub_enabled(monkeypatch):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_calculate_cost_stub_is_billed_at_the_fixed_price_when_enabled(stub_enabled):
    cost = calculate_cost(
        "stub", "lat50-out200", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 2.0  # $1/1M input + $1/1M output at the default STUB_PRICE_PER_1M


def test_calculate_cost_stub_is_free_when_flag_disabled():
    cost = calculate_cost(
        "stub", "lat50-out200", prompt_tokens=1_000_000, completion_tokens=1_000_000
    )
    assert cost == 0.0


def test_enforce_pricing_policy_never_rejects_stub_regardless_of_miss_policy(
    stub_enabled, miss_policy
):
    miss_policy("reject")
    assert enforce_pricing_policy("stub", "lat50-out200-itl5") is None


def test_enforce_pricing_policy_stub_never_increments_unpriced_metric(stub_enabled):
    before = unpriced_model_total.labels(provider="stub", outcome="rejected")._value.get()
    enforce_pricing_policy("stub", "lat50-out200")
    after = unpriced_model_total.labels(provider="stub", outcome="rejected")._value.get()
    assert after == before
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/accounts/test_accounting.py -k stub -v`
Expected: FAIL - `test_calculate_cost_stub_is_billed_at_the_fixed_price_when_enabled` gets `0.0` instead of `2.0` (falls through to the existing `BILLED_PROVIDERS` miss logic, and `"stub"` is not in `BILLED_PROVIDERS`).

- [ ] **Step 3: Implement**

In `gatekeep/accounts/accounting.py`, add the constant near the top (after the
`_MISS_OUTCOME` dict):

```python
# Fixed per-1M-token USD price applied to both input and output tokens for
# every `stub/*` model when `loadtest_stub_enabled` is true. Deliberately a
# flat rate rather than a pricing-table entry - `stub` is never added to
# BILLED_PROVIDERS (there are unboundedly many stub/* variants and the
# exact-match table cannot wildcard them) - so a load-test scenario can
# compute the exact request count at which a budget cap should trip. Nominal
# value, not tied to any real provider's price; see
# docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §3.
STUB_PRICE_PER_1M = 1.0
```

Update `calculate_cost` to check the stub branch first:

```python
def calculate_cost(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate the USD cost of a completion from its provider, model, and token counts.

    ... (existing docstring, plus:)

    When `provider == "stub"` and `Settings.loadtest_stub_enabled` is true,
    cost is `STUB_PRICE_PER_1M` per 1M tokens on both input and output,
    checked before any pricing-table lookup - `stub` is deliberately never a
    table entry (see routing.pricing.BILLED_PROVIDERS) and this branch is
    unreachable when the flag is false, since `stub` is then not even a
    registered provider (see gatekeep.app._build_providers).
    """
    settings = get_settings()
    if provider == "stub" and settings.loadtest_stub_enabled:
        return (prompt_tokens / 1_000_000 * STUB_PRICE_PER_1M) + (
            completion_tokens / 1_000_000 * STUB_PRICE_PER_1M
        )
    price = get_pricing_table().lookup(provider, model)
    if price is not None:
        return price.cost(prompt_tokens, completion_tokens)
    if provider in BILLED_PROVIDERS and settings.pricing_miss_policy == "ceiling":
        ceiling = settings.pricing_ceiling_per_1m
        return (prompt_tokens / 1_000_000 * ceiling) + (completion_tokens / 1_000_000 * ceiling)
    return 0.0
```

(The old body called `get_settings()` only inside the `if price is not None`
branch's `else` path; hoisting it to the top is safe since `get_settings` is
`@lru_cache`d and side-effect-free.)

- [ ] **Step 4: Run and verify pass**

Run: `pytest tests/accounts/test_accounting.py -v`
Expected: PASS (all accounting tests, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add gatekeep/accounts/accounting.py tests/accounts/test_accounting.py
git commit -m "feat(loadtest): bill stub requests at a fixed per-1M-token price"
```

---

### Task 7: Budget enforcement integration test

**Files:**
- Create: `tests/test_loadtest_budget_enforcement.py`

**Interfaces:**
- Consumes: `StubProvider` (Task 3), stub registration via `monkeypatch.setitem` (Task 5 pattern), `STUB_PRICE_PER_1M` (Task 6, monkeypatched to a large value so a tiny budget trips in a handful of requests), `require_budget` / `get_period_spend` (existing, `gatekeep/middleware/budget.py`).

This is a pure integration test (no new production code) proving the whole
chain end to end: a real HTTP request against a real low-budget account,
through `require_budget`, produces a `429 budget_exceeded_error` at exactly
the predicted request count, with the Redis spend counter and the
`request_logs` DB aggregate agreeing.

- [ ] **Step 1: Write the test**

Create `tests/test_loadtest_budget_enforcement.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import func, select

import gatekeep.app as app_module
from gatekeep.accounts import accounting
from gatekeep.accounts.auth_keys import generate_key, hash_key
from gatekeep.config import get_settings
from gatekeep.middleware.budget import get_period_spend
from gatekeep.middleware.ratelimit import get_redis
from gatekeep.providers.stub import StubProvider
from gatekeep.storage.models import ApiKey, RequestLog
from tests.helpers import create_account


async def test_stub_budget_enforcement_blocks_at_predicted_spend(client, session, monkeypatch):
    monkeypatch.setenv("LOADTEST_STUB_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setitem(app_module._providers, "stub", StubProvider())
    # Inflate the fixed stub price so a tiny budget trips in a handful of
    # requests instead of needing an enormous output-token count.
    monkeypatch.setattr(accounting, "STUB_PRICE_PER_1M", 1000.0)

    # cost/request = (prompt_tokens + completion_tokens) / 1e6 * 1000
    #   prompt_tokens == 2: StubProvider._payload_text joins the (empty)
    #   system string and the "ping N" message with "\n", giving "\nping N"
    #   (7 chars for a single-digit N) - estimate_tokens rounds that up to
    #   ceil(7/4) == 2.
    #   completion_tokens == 10 (stub/lat1-out10)
    #   => (2 + 10) / 1_000_000 * 1000 == 0.012 per request
    # budget 0.032 allows exactly 3 requests (0.036 after the 3rd is already
    # >= budget) before the 4th is blocked.
    account = await create_account(session, monthly_budget_usd=0.032)
    raw = generate_key()
    session.add(ApiKey(name="k", key_hash=hash_key(raw), account_id=account.id))
    await session.commit()

    # The prompt varies per request (rather than a fixed "ping") so every
    # call is a cache miss - gatekeep's exact-response cache would otherwise
    # serve requests 2-5 from cache after the first, and a cache hit
    # deliberately contributes $0 to the budget counter (see
    # accounting.log_request's docstring), which would never trip the cap.
    # "ping 0".."ping 4" are all 6 characters, so the token-count arithmetic
    # above is unchanged across iterations.
    statuses = []
    for i in range(5):
        body = {
            "model": "stub/lat1-out10",
            "messages": [{"role": "user", "content": f"ping {i}"}],
        }
        r = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {raw}"}, json=body
        )
        statuses.append(r.status_code)

    assert statuses == [200, 200, 200, 429, 429]

    spend_from_db = (
        await session.execute(
            select(func.sum(RequestLog.cost_usd)).where(RequestLog.account_id == account.id)
        )
    ).scalar_one()
    assert spend_from_db == pytest.approx(0.036)

    redis = get_redis()
    counter_spend = await get_period_spend(session, redis, account_id=account.id)
    assert counter_spend == pytest.approx(spend_from_db)
```

- [ ] **Step 2: Run and verify it passes**

Run: `pytest tests/test_loadtest_budget_enforcement.py -v`
Expected: PASS. If it fails on the `statuses` assertion, print `statuses` and
re-check the arithmetic above against the current `estimate_tokens`/budget
comparison semantics before adjusting the budget/token constants - do not
loosen the assertion to "some succeed, some don't".

- [ ] **Step 3: Commit**

```bash
git add tests/test_loadtest_budget_enforcement.py
git commit -m "test(loadtest): verify budget enforcement blocks at predicted stub spend"
```

---

### Task 8: `loadtest` packaging extra

**Files:**
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `pip install -e ".[loadtest]"` installs `locust`, consumed by Task 11's `locustfile.py` and the `justfile`'s `loadtest` targets (Task 12).

- [ ] **Step 1: Implement**

In `pyproject.toml`, add a new group under `[project.optional-dependencies]`,
after the existing `demo` group:

```toml
loadtest = [
    "locust>=2.31",
]
```

- [ ] **Step 2: Verify**

Run: `pip install -e ".[loadtest]"` then `locust --version`
Expected: prints a Locust version string with no errors.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(loadtest): add locust as an optional dependency"
```

---

### Task 9: Load-test compose override

**Files:**
- Create: `loadtest/docker-compose.loadtest.yml`

**Interfaces:**
- Consumes: `LOADTEST_STUB_ENABLED` (Task 1), the base `docker-compose.yml`'s `gateway` service (env var names match `Settings` fields uppercased, e.g. `RATE_LIMIT_TOKENS_PER_MIN` -> `Settings.rate_limit_tokens_per_min`).
- Produces: the running stack Task 10/11 target via `TARGET_HOST=http://localhost:8100` (gateway's compose-mapped port, unchanged from the base file).

- [ ] **Step 1: Implement**

Create `loadtest/docker-compose.loadtest.yml`:

```yaml
# Load-test override for docker-compose.yml. Usage:
#   just loadtest-up    # docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d
# See loadtest/README.md for the full runbook.
#
# Postgres/Redis/Prometheus/Grafana are unchanged from the base compose file
# (docker compose merges services by name; not redeclaring a service here
# means "use the base definition as-is").
services:
  gateway:
    environment:
      DATABASE_URL: postgresql+asyncpg://gatekeep:gatekeep@postgres:5432/gatekeep
      REDIS_URL: redis://redis:6379/0
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-sk-ant-unused-loadtest}
      OPENAI_API_KEY: ${OPENAI_API_KEY:-}
      GOOGLE_API_KEY: ${GOOGLE_API_KEY:-}
      OLLAMA_HOST: http://ollama:11434
      DEFAULT_MODEL: ${DEFAULT_MODEL:-llama3.2}
      # Registers the stub provider and stub/ model routing - see
      # gatekeep/config.py's loadtest_stub_enabled and
      # gatekeep/app.py's _build_providers.
      LOADTEST_STUB_ENABLED: "true"
      # Rate limiting is one process-wide token bucket shared by every
      # account (see the design doc's "Relevant existing architecture").
      # Raised well above any scenario's target RPS - keeping the same
      # capacity/refill ratio as the defaults (100/60 and 300/60) - so the
      # limiter is never the throughput/latency bottleneck. The
      # BreakingPointUser Locust scenario still exercises 429s by ramping
      # past this raised ceiling.
      RATE_LIMIT_TOKENS_PER_MIN: "600000"
      RATE_LIMIT_REFILL_RATE: "10000"
      PRE_AUTH_RATE_LIMIT_TOKENS_PER_MIN: "1800000"
      PRE_AUTH_RATE_LIMIT_REFILL_RATE: "30000"
      # PRICING_MISS_POLICY intentionally left at its default ("reject"):
      # stub traffic is never treated as unpriced regardless of this policy
      # (see gatekeep/accounts/accounting.py's STUB_PRICE_PER_1M branch), so
      # it only ever governs real billed-provider models, which load-test
      # scenarios never send.
    # Single gateway worker by default, for clean per-request overhead
    # numbers in the throughput/latency/breaking-point scenarios. For a
    # second capacity pass measuring multi-worker scaling, uncomment:
    #   command: ["uvicorn", "gatekeep.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

- [ ] **Step 2: Verify**

Run: `docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml config`
Expected: prints the fully merged compose config with no errors, and the
`gateway.environment` block shows `LOADTEST_STUB_ENABLED: "true"` and the
raised rate-limit values.

- [ ] **Step 3: Commit**

```bash
git add loadtest/docker-compose.loadtest.yml
git commit -m "feat(loadtest): add docker-compose override for load testing"
```

---

### Task 10: Key bootstrap script

**Files:**
- Create: `loadtest/bootstrap.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `gatekeep.accounts.account_service.create_account` / `.create_key` (existing).
- Produces: `loadtest/keys.json` with shape `{"pool": [<raw key>, ...], "budget": [<raw key>, ...]}`, consumed by Task 11's `locustfile.py`.

- [ ] **Step 1: Implement**

Create `loadtest/bootstrap.py`:

```python
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
    of the enforcement scenario (design doc §6.4)."""
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
    """Mint both key pools and write them to keys.json."""
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
```

Append to `.gitignore`:

```
loadtest/keys.json
loadtest/results/*.csv
loadtest/results/*.html
```

- [ ] **Step 2: Verify**

With the load-test stack up (`docker compose -f docker-compose.yml -f
loadtest/docker-compose.loadtest.yml up -d`, `DATABASE_URL` in the host
`.env` pointing at `localhost:5432` as it already does for
`scripts/create_key.py`), run:

```bash
python loadtest/bootstrap.py --pool-size 5 --budget-keys 2 --budget-usd 0.50
cat loadtest/keys.json
```

Expected: prints `wrote 5 pool keys and 2 budget keys to .../loadtest/keys.json`,
and the file contains a JSON object with `"pool"` (5 entries) and `"budget"`
(2 entries) arrays of `gk-...`-shaped raw keys. Re-run the same command and
confirm it succeeds a second time without a name-collision error.

- [ ] **Step 3: Commit**

```bash
git add loadtest/bootstrap.py .gitignore
git commit -m "feat(loadtest): add key bootstrap script"
```

---

### Task 11: Locust harness

**Files:**
- Create: `loadtest/locustfile.py`

**Interfaces:**
- Consumes: `loadtest/keys.json` (Task 10), `stub/lat50-out200` / `stub/lat50-out200-itl5` model strings (Task 2's `parse_stub_model` contract), `TARGET_HOST` env var.
- Produces: `ThroughputUser`, `LatencyUser`, `BreakingPointUser`, `EnforcementUser` Locust classes, named on the `locust` CLI by the `justfile`'s `loadtest <scenario>` target (Task 12).

- [ ] **Step 1: Implement**

Create `loadtest/locustfile.py`:

```python
"""Locust load-testing harness for gatekeep's own request-handling overhead.

Drives the gateway through the OpenAI-compatible /v1/chat/completions
endpoint using the in-app stub provider (gatekeep/providers/stub.py, gated
by LOADTEST_STUB_ENABLED - see loadtest/docker-compose.loadtest.yml) so
latency and cost are isolated from any real upstream. See
docs/superpowers/specs/2026-08-30-load-testing-harness-design.md §6.

Run one scenario at a time by naming its User class on the command line:

    locust -f loadtest/locustfile.py ThroughputUser --headless \\
        -u 400 -r 20 -t 5m --host http://localhost:8100 \\
        --csv loadtest/results/throughput

`just loadtest <ClassName>` wraps this (see justfile). ThroughputUser and
BreakingPointUser define their own LoadTestShape and ignore -u/-r; LatencyUser
and EnforcementUser use -u/-r directly.

Note: for a shape-driven scenario (ThroughputUser, BreakingPointUser),
Locust runs the shape's full stage list to completion before -t/--run-time
is even consulted (locust/main.py's start_automatic_run joins the shape
greenlet first) - -t above bounds LatencyUser/EnforcementUser runs, but has
no effect here. To stop a shape-driven run early, interrupt it with
Ctrl+C/SIGINT; Locust catches that around the shape join and shuts down
gracefully, printing the summary as usual.

Environment:
    TARGET_HOST: base URL of the gateway (default http://localhost:8100).
    LOADTEST_KEYS_PATH: path to bootstrap.py's keys.json (default loadtest/keys.json).
"""

from __future__ import annotations

import json
import os
import random
import sys
import uuid
from pathlib import Path

from locust import HttpUser, LoadTestShape, between, constant_pacing, task

# Locust picks the *first* non-abstract LoadTestShape subclass it finds in
# the locustfile as THE shape for the run, unconditionally - it does not
# consult which User class was named on the CLI (confirmed against
# locust 2.46.4's locust/util/load_locustfile.py:is_shape_class and
# locust/main.py:merge_locustfiles_content, where
# `shape_class = list(available_shape_classes.values())[0]`). Since this
# file defines two shapes (ThroughputShape, BreakingPointShape) alongside
# four User classes, without a guard, running `LatencyUser` or
# `EnforcementUser` headless would silently pick up ThroughputShape and
# ignore -u/-r, and BreakingPointShape would never be selectable at all.
# Mark each shape `abstract` (excluded from discovery - see
# LoadTestShapeMeta) unless its matching User class name is present on the
# command line. Only supports the single-class CLI invocation documented
# above; Web UI mode without a class argument sees no shape.
_SELECTED_CLASSES = set(sys.argv[1:])

# Cardinality guardrail (design doc §7): gateway_overhead_seconds,
# request_duration_seconds, and provider_duration_seconds are labeled by
# `model`, and the stub model string encodes latency/size/ITL - so every
# scenario below reuses exactly these two fixed model strings rather than
# generating parameterizations dynamically. Do not add more without also
# updating loadtest/README.md's Prometheus panel list.
MODEL_NON_STREAM = "stub/lat50-out200"
MODEL_STREAM = "stub/lat50-out200-itl5"

_KEYS_PATH = Path(os.environ.get("LOADTEST_KEYS_PATH", Path(__file__).parent / "keys.json"))
_CACHE_HIT_PROMPT = "the quick brown fox jumps over the lazy dog"


def _load_keys() -> dict[str, list[str]]:
    """Load the pool/budget key lists written by loadtest/bootstrap.py."""
    if not _KEYS_PATH.exists():
        raise FileNotFoundError(
            f"{_KEYS_PATH} not found - run `just loadtest-bootstrap` first "
            "(see loadtest/README.md)."
        )
    return json.loads(_KEYS_PATH.read_text())


_KEYS = _load_keys()


def _headers(raw_key: str) -> dict[str, str]:
    """Build the Authorization header for one gatekeep API key."""
    return {"Authorization": f"Bearer {raw_key}"}


def _body(*, model: str, cache_hit: bool, stream: bool) -> dict:
    """Build a chat-completion request body.

    A cache-hit request always sends the exact same prompt (stable hash, so
    repeated calls hit gatekeep's exact-response cache); a cache-miss
    request appends a fresh UUID so every call is a distinct, uncached
    prompt.
    """
    prompt = _CACHE_HIT_PROMPT if cache_hit else f"{_CACHE_HIT_PROMPT} {uuid.uuid4()}"
    return {"model": model, "messages": [{"role": "user", "content": prompt}], "stream": stream}


class _StubTasks:
    """Shared task bodies mixed into every HttpUser class below.

    Each of the request shapes (non-stream/stream x cache-hit/cache-miss) is
    its own @task so Locust's per-task stats break them out individually.
    Weighted 3:1:2 (non-stream-miss : non-stream-hit : stream-miss) so the
    common non-streaming, cache-miss path dominates traffic while still
    exercising the cache and streaming paths every run.
    """

    def _post(self, *, cache_hit: bool, stream: bool) -> None:
        key = random.choice(self.keys)
        model = MODEL_STREAM if stream else MODEL_NON_STREAM
        body = _body(model=model, cache_hit=cache_hit, stream=stream)
        with self.client.post(
            "/v1/chat/completions",
            json=body,
            headers=_headers(key),
            stream=stream,
            catch_response=True,
        ) as response:
            if stream and response.status_code == 200:
                # Drain the SSE body so the connection's full duration - not
                # just headers - counts toward this task's response time.
                # A dropped connection mid-stream (e.g. the gateway closing
                # early under heavy load) raises requests.exceptions.
                # ChunkedEncodingError from inside iter_lines() -
                # catch_response only guards the initial request, not body
                # consumption inside this `with` block, so left unguarded
                # this would propagate as an unhandled traceback out of the
                # task instead of being recorded as a failed request.
                try:
                    for _ in response.iter_lines():
                        pass
                except Exception as exc:  # noqa: BLE001
                    response.failure(str(exc))

    @task(3)
    def non_stream_cache_miss(self) -> None:
        self._post(cache_hit=False, stream=False)

    @task(1)
    def non_stream_cache_hit(self) -> None:
        self._post(cache_hit=True, stream=False)

    @task(2)
    def stream_cache_miss(self) -> None:
        self._post(cache_hit=False, stream=True)


# Locust's UserMeta only auto-collects @task-decorated methods that live
# directly in a User subclass's own class body (or in a base that has
# already been through UserMeta, i.e. is itself a User/TaskSet) - installed
# locust 2.46.4 silently drops the tasks of a plain mixin class like
# _StubTasks otherwise (verified against locust.user.task.
# get_tasks_from_base_classes). Compute the weighted task list explicitly
# so it's picked up via `hasattr(base, "tasks")` when User subclasses below
# list _StubTasks as a base.
_StubTasks.tasks = [
    method
    for method in vars(_StubTasks).values()
    if hasattr(method, "locust_task_weight")
    for _ in range(method.locust_task_weight)
]


class ThroughputUser(_StubTasks, HttpUser):
    """Goal 1 - throughput/capacity: paired with ThroughputShape to find max
    sustainable RPS before p95 gateway overhead climbs or errors appear."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)
    keys = _KEYS["pool"]


class ThroughputShape(LoadTestShape):
    """Step-ramp for ThroughputUser: +20 users every 30s up to 400.

    Watch gateway_overhead_seconds p95 and error rate in Grafana as each
    step lands; the step where p95 first climbs sharply is the practical
    capacity ceiling (see loadtest/README.md).
    """

    abstract = "ThroughputUser" not in _SELECTED_CLASSES

    stages = [
        {"duration": 30, "users": 20, "spawn_rate": 20},
        {"duration": 60, "users": 40, "spawn_rate": 20},
        {"duration": 90, "users": 80, "spawn_rate": 40},
        {"duration": 120, "users": 160, "spawn_rate": 80},
        {"duration": 150, "users": 240, "spawn_rate": 80},
        {"duration": 180, "users": 320, "spawn_rate": 80},
        {"duration": 210, "users": 400, "spawn_rate": 80},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None


class LatencyUser(_StubTasks, HttpUser):
    """Goal 2 - latency SLO: fixed, moderate concurrency; read
    gateway_overhead_seconds percentiles from Prometheus/Grafana during the
    run and compare against the draft SLOs in loadtest/README.md."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = constant_pacing(1)
    keys = _KEYS["pool"]


class BreakingPointUser(_StubTasks, HttpUser):
    """Goal 3 - breaking point: paired with BreakingPointShape, ramps well
    past ThroughputUser's ceiling to observe failure modes (connection-pool
    exhaustion, timeouts, 429s) rather than to find a clean capacity
    number."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)
    keys = _KEYS["pool"]


class BreakingPointShape(LoadTestShape):
    """Faster, higher-ceiling step-ramp than ThroughputShape, for BreakingPointUser."""

    abstract = "BreakingPointUser" not in _SELECTED_CLASSES

    stages = [
        {"duration": 20, "users": 50, "spawn_rate": 50},
        {"duration": 40, "users": 200, "spawn_rate": 100},
        {"duration": 60, "users": 500, "spawn_rate": 150},
        {"duration": 80, "users": 1000, "spawn_rate": 200},
        {"duration": 100, "users": 1500, "spawn_rate": 200},
        {"duration": 120, "users": 2000, "spawn_rate": 200},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["duration"]:
                return stage["users"], stage["spawn_rate"]
        return None


class EnforcementUser(HttpUser):
    """Goal 4 - enforcement under concurrency: saturates one low-budget key
    (from bootstrap.py's "budget" pool) so the budget block fires at the
    predicted spend under real concurrent load (rate-limit exactness is
    covered by BreakingPointUser instead - see design doc §6.4)."""

    host = os.environ.get("TARGET_HOST", "http://localhost:8100")
    wait_time = between(0, 0)

    def on_start(self) -> None:
        """Pin this simulated user to one randomly-chosen low-budget key for
        its whole run, so concurrent users still converge on saturating a
        small, known set of accounts."""
        self.key = random.choice(_KEYS["budget"])

    @task
    def hammer_low_budget_key(self) -> None:
        body = _body(model=MODEL_NON_STREAM, cache_hit=False, stream=False)
        self.client.post("/v1/chat/completions", json=body, headers=_headers(self.key))
```

- [ ] **Step 2: Verify**

With the load-test stack up and `loadtest/keys.json` populated (Task 10),
run each scenario for a short smoke duration and confirm it starts, sends
requests, and produces output with no tracebacks:

```bash
locust -f loadtest/locustfile.py LatencyUser --headless -u 5 -r 5 -t 15s --host http://localhost:8100
locust -f loadtest/locustfile.py ThroughputUser --headless -t 40s --host http://localhost:8100
locust -f loadtest/locustfile.py EnforcementUser --headless -u 3 -r 3 -t 15s --host http://localhost:8100
```

Expected: each run prints Locust's summary table with `Failures` at or near
0% for `LatencyUser`/`ThroughputUser` (`EnforcementUser` is expected to show
429s once its low-budget key's cap is hit - that is the scenario working
correctly, not a bug).

- [ ] **Step 3: Commit**

```bash
git add loadtest/locustfile.py
git commit -m "feat(loadtest): add Locust scenarios for throughput/latency/breaking-point/enforcement"
```

---

### Task 12: Justfile targets

**Files:**
- Modify: `justfile`

**Interfaces:**
- Consumes: `loadtest/docker-compose.loadtest.yml` (Task 9), `loadtest/bootstrap.py` (Task 10), `loadtest/locustfile.py` (Task 11).

- [ ] **Step 1: Implement**

Append to `justfile`, after the `# --- Database migrations ---` section:

```just
# --- Load testing (see loadtest/README.md) ---

# Bring up the full stack with the load-test override (stub provider
# enabled, rate limits raised well above target load)
loadtest-up:
    docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d --build

# Mint fresh load-test API keys into loadtest/keys.json (safe to re-run)
loadtest-bootstrap:
    python loadtest/bootstrap.py

# Run one scenario headless against the running stack, e.g.:
#   just loadtest ThroughputUser
#   just loadtest LatencyUser
#   just loadtest BreakingPointUser
#   just loadtest EnforcementUser
# -u/-r are ignored by ThroughputUser/BreakingPointUser (their own
# LoadTestShape governs concurrency instead) and used directly by
# LatencyUser/EnforcementUser.
loadtest scenario:
    locust -f loadtest/locustfile.py {{scenario}} --headless \
        -u 400 -r 20 -t 5m --host http://localhost:8100 \
        --csv loadtest/results/{{scenario}}

# Tear down the load-test stack
loadtest-down:
    docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml down
```

- [ ] **Step 2: Verify**

Run: `just --list`
Expected: `loadtest-up`, `loadtest-bootstrap`, `loadtest`, `loadtest-down`
appear in the listing with their comments.

- [ ] **Step 3: Commit**

```bash
git add justfile
git commit -m "chore(loadtest): add justfile targets for the load-test workflow"
```

---

### Task 13: Runbook and results template

**Files:**
- Create: `loadtest/README.md`
- Create: `loadtest/results/.gitkeep`

**Interfaces:**
- Consumes: every prior task (documents the whole workflow end to end).

- [ ] **Step 1: Implement**

Create `loadtest/results/.gitkeep` (empty file, so the directory is tracked
even though its `*.csv`/`*.html` contents are gitignored per Task 10).

Create `loadtest/README.md`:

```markdown
# Load-Testing Gatekeep

Measures the gateway's own request-handling overhead (auth, rate limiting,
budget checks, cache lookups, cost accounting, the per-request Postgres
write, routing) in isolation from any real upstream provider, using an
in-app stub provider. See
`docs/superpowers/specs/2026-08-30-load-testing-harness-design.md` for the
full design.

**Never set `LOADTEST_STUB_ENABLED=true` outside this local workflow.**

## Setup

```bash
just loadtest-up          # bring up postgres/redis/ollama/prometheus/grafana
                           # + gateway with the stub enabled and rate limits raised
just loadtest-bootstrap    # mint keys into loadtest/keys.json (git-ignored)
pip install -e ".[loadtest]"
```

Grafana is at `http://localhost:3000` (anonymous viewer access), Prometheus
at `http://localhost:9090`.

## Running a scenario

```bash
just loadtest ThroughputUser     # goal 1: max sustainable RPS
just loadtest LatencyUser        # goal 2: latency SLO at fixed concurrency
just loadtest BreakingPointUser  # goal 3: ramp past capacity, observe failure modes
just loadtest EnforcementUser    # goal 4: budget cap under concurrency
```

Each run writes a Locust CSV to `loadtest/results/<ScenarioName>_*.csv`
(git-ignored).

Every scenario sends only two stub model strings -
`stub/lat50-out200` (non-streaming and cache paths) and
`stub/lat50-out200-itl5` (streaming) - deliberately fixed rather than swept,
to avoid minting new `model` label values on the process-lifetime Prometheus
histograms below. Do not add more without checking that guardrail still
holds.

## What to read, per scenario

**Client-side (Locust's own console/CSV output):** RPS, latency percentiles,
failure rate.

**Server-side (Grafana / Prometheus queries):**

| Scenario | Panels / queries |
|---|---|
| ThroughputUser | `histogram_quantile(0.95, rate(gatekeep_gateway_overhead_seconds_bucket[1m]))`; `rate(gatekeep_request_duration_seconds_count{path=~".+"}[1m])` (RPS); error rate from Locust. The step where p95 first climbs sharply is the practical capacity ceiling. |
| LatencyUser | `histogram_quantile(0.5\|0.95\|0.99, rate(gatekeep_gateway_overhead_seconds_bucket[5m]))`, split by whether the request hit cache (`gatekeep_cache_exact_hits`/`gatekeep_cache_exact_misses` rates) - compare against the draft SLOs below. |
| BreakingPointUser | Locust failure rate and error types; `rate(gatekeep_rate_limit_rejections_total[1m])` (confirm 429s appear once aggregate RPS exceeds the raised process-wide limit, with no over-admission before it); Postgres/Redis connection and error metrics (host-level or `docker compose logs postgres redis`). |
| EnforcementUser | HTTP 429 `budget_exceeded_error` responses in Locust's failure log, appearing once the low-budget key's account crosses `monthly_budget_usd`; confirm no further 200s after the block starts. |

## Draft SLOs (placeholders - replace with the first baseline's numbers)

- Cache-hit gateway overhead p95 < 15 ms
- Stub non-streaming overhead p95 < 25 ms
- Error rate < 0.1% below capacity

## Results log

Record one row per baseline run. Keep this table in this file (not a
separate results file) so history and methodology stay together.

| Date | Scenario | Max sustainable RPS | p50 / p95 / p99 overhead (ms) | Error rate | Notes |
|---|---|---|---|---|---|
| | | | | | |

## Scaling to multiple gateway workers

The default `loadtest-up` runs the gateway single-worker, for clean
per-request overhead numbers. For a second capacity pass, edit the
`command:` override commented in `loadtest/docker-compose.loadtest.yml` to
run multiple uvicorn workers, then re-run `just loadtest-up` and repeat the
scenario.

## Running against a different host

`TARGET_HOST` (read by `loadtest/locustfile.py`) and `LOADTEST_KEYS_PATH`
make the harness host-agnostic - pointing both at a staging deployment
instead of `localhost:8100` is a config change, not new code. Do this only
against a host that also has `LOADTEST_STUB_ENABLED=true` and its own
`loadtest/keys.json` minted against it.
```

- [ ] **Step 2: Verify**

Run: `just loadtest-up && just loadtest-bootstrap && just loadtest LatencyUser`
(with `-t` shortened for a smoke run, per Task 11's verification) and confirm
the resulting `loadtest/results/LatencyUser_stats.csv` exists and Grafana at
`localhost:3000` shows non-zero `gatekeep_gateway_overhead_seconds` activity
for the run's duration. Then `just loadtest-down`.

- [ ] **Step 3: Commit**

```bash
git add loadtest/README.md loadtest/results/.gitkeep
git commit -m "docs(loadtest): add runbook and results template"
```

---

## Final Verification

- [ ] Run the full suite: `pytest` - expect all green, including every new
  test file added above.
- [ ] Run `ruff check .` and `ruff format --check .` - expect no findings.
- [ ] Confirm `git grep -n "loadtest_stub_enabled" gatekeep/config.py` shows
  the default is `False` (never flipped anywhere in production code paths).
- [ ] Walk through Task 13's manual verification once end to end (`loadtest-up`
  -> `loadtest-bootstrap` -> one short scenario per Locust class ->
  `loadtest-down`) to confirm the whole workflow works, not just its pieces.
