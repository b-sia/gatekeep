# Gatekeep Roadmap

Post-Phase-3 roadmap, ordered by implementation difficulty (easiest first).
Source: `docs/superpowers/specs/2026-07-05-gatekeep-design.md` (Roadmap
section) plus items explicitly deferred out of Phase 3
(`docs/superpowers/plans/2026-07-13-gatekeep-phase-3-eval-gate.md`).

Already shipped, off the list: **model-downgrade routing** — implemented in
`gatekeep/routing.py:select_model`, wired into `gatekeep/app.py`.

## 1. Additional upstream providers (OpenAI, Google)

Add `OpenAIProvider`/`GoogleProvider` alongside today's `AnthropicProvider`/
`OllamaProvider`, following the existing `providers/base.py` interface.
Routed via an explicit `openai/`/`google/` model prefix so today's default
alias table (bare `gpt-4` → Claude, for the zero-config demo) is untouched.

**Plan:** `docs/superpowers/plans/2026-07-20-gatekeep-additional-providers.md`

## 2. Anthropic-native `/v1/messages` passthrough

A second API surface matching the real Anthropic Messages API shape, for
clients using the native `anthropic` SDK. Reuses all existing middleware
(auth, rate limit, tiered cache, cost-aware routing, accounting) — gatekeep's
internal payload representation is already Anthropic-shaped, so this needs
far less translation than the OpenAI-compat endpoint did.

**Plan:** `docs/superpowers/plans/2026-07-20-gatekeep-native-messages-endpoint.md`

## 3. Budgets & quotas (per-key spend caps + alerting)

Extend the existing per-key Redis token-bucket pattern (`middleware/ratelimit.py`)
and per-request cost accounting (`accounting.py`) with a cost-based hard cap
and an alert hook. No plan written yet.

## 4. Automated judge-criteria generation from curated cases

Today's eval gate scores against a fixed generic rubric string. Self-contained
inside `evals.py` — no architectural changes, but a genuine prompt-engineering
problem (how curated cases become a rubric). No plan written yet.

## 5. Custom web dashboard

First-party UI for cost/usage/eval history, beyond Grafana. Mostly read-only
endpoints over data that already exists, plus a frontend (can lean on
`demo/` as a UI template). Large in scope/time, low in technical risk. No
plan written yet.

## 6. A/B testing prompt versions in production (partial traffic split)

Extend `prompts.py`'s resolve-at-request-time logic to route a percentage of
traffic to a candidate version, plus comparative metrics to evaluate the
split — today's promotion model is binary (promote/rollback). Touches
request-time routing correctness, bigger blast radius than items 1-5. No
plan written yet.

## 7. Per-organization eval suites / multi-tenant isolation

A new tenancy dimension cutting across auth, keys, prompts, evals, and
curation — significant data-model and migration work. No plan written yet.

## 8. Canary rollout with automatic rollback

Gradual prompt-version rollout with automatic rollback on live metric/quality
regression. Needs Prometheus/Grafana-integrated live monitoring, an automated
decision loop, and a safe rollback path — safety-critical and the least like
anything already built. No plan written yet.
