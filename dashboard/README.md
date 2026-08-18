# Dashboard

A first-party React/TypeScript SPA, built with Vite and Tailwind, served by the
gateway at `http://localhost:8100/dashboard`.

This is Gatekeep's **analytics surface**. Everything it shows is computed exactly
from `request_logs` and the prompt/eval tables, filterable by model and time
window:

- Cost and usage over time, broken down by model
- Cache savings and hit rate
- Latency - end-to-end, provider, gateway overhead, and TTFT - with end-to-end
  broken down by path, model, key, and prompt
- Prompt version history
- Eval run history

Per-key and per-prompt latency attribution lives here rather than in Prometheus
because `key_id` is deliberately not a metric label; see
[`gatekeep/observability/README.md`](../gatekeep/observability/README.md).

## Auth

The dashboard keeps a shared roster of saved Gatekeep identities - the same kind of
key used for `/v1/chat/completions`, plus the account name and operator flag
resolved from `/me` when the key is added - in the browser's `localStorage`. Each
browser tab tracks its own active identity independently, as a per-tab pointer in
`sessionStorage`, so two tabs can be logged in as two different accounts at the
same time.

A new tab always starts logged out and shows the identity picker, even if
identities are already saved in the roster - you pick (or add) one per tab. The
active identity's key is sent as a bearer token to the dashboard's own read-only
API under `/dashboard/api/*`, which is served by `gatekeep/api/dashboard.py` and
requires a valid key on every endpoint.

If a saved key is rejected by the gateway (e.g. revoked), that roster entry is
marked invalid and offers a re-authenticate action instead of being removed
automatically.

Any valid key can read the whole dataset - the dashboard API is not scoped to the
calling key. Treat dashboard access as an operator-level privilege.

## Comparing against Grafana

`/dashboard` reads slightly lower than Grafana for identical traffic.
`request_logs.duration_ms` stops just before the accounting write, so it excludes
JSON serialization and the socket write, whereas
`gatekeep_request_duration_seconds` covers the full ASGI span.

## Local development

Run the Vite dev server separately from the gateway:

```bash
cd dashboard && npm install && npm run dev
```

This gives hot reload and proxies `/dashboard/api` requests to
`http://localhost:8100`, so the gateway still needs to be running
(`docker-compose up -d`).

The gateway serves the built SPA from `dashboard/dist/`, which the Docker image
builds. After changing the frontend, rebuild before testing against the container:

```bash
cd dashboard && npm run build
```
