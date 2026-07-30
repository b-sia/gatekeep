# Dashboard Redesign

## Problem

The current cost/usage/eval dashboard (`demo/static/dashboard.{html,css,js}`) is a
vanilla HTML/CSS/JS page served by the demo chat app (`demo/app.py`), which proxies
every call to Gatekeep's `/dashboard/api/...` endpoints so it can attach a
server-side API key without exposing it to the browser. Visually it's plain
stacked cards/tables with a hand-rolled SVG bar chart - functional for a POC but
not representative of a real product surface, and it's misplaced: it lives under
the demo app (whose job is to show *how a client integrates with Gatekeep*)
rather than being a feature of Gatekeep itself.

## Goals

- Rebuild the dashboard as a polished, production-feeling single-page app.
- Make it a first-party Gatekeep feature: its own top-level directory, served
  directly by `gatekeep/app.py`, not tucked under `demo/`.
- Only show panels/metrics backed by data Gatekeep actually collects today.
  No credits, agents, notifications, org/project switcher, or error-count
  cards - none of that exists in the data model.
- Small, additive backend changes are in scope where the underlying column
  already exists (API key names, prompt/completion token split) but isn't
  yet exposed via the dashboard API.

## Non-goals

- No sidebar / multi-page navigation. The backend only supports one real
  page's worth of data (usage summary, timeseries, prompts, evals); a
  sidebar linking to sections with no backing feature would be fake chrome.
- No session/login auth system. Gatekeep's only auth primitive is the API
  key; the dashboard uses that directly rather than inventing a new auth
  subsystem.
- No changes to the demo chat app's own UI (`demo/static/index.html` etc.) -
  only the removal of its now-redundant dashboard proxy routes and static
  files.
- No budget/spend-cap visualization. `ApiKey.monthly_budget_usd` exists but
  surfacing it is a separate feature decision, not part of this redesign.

## Current data available (ground truth for what can be displayed)

From `gatekeep/api/dashboard.py` / `gatekeep/models.py`:

- **Usage summary** (`GET /dashboard/api/usage/summary`): request count,
  total tokens, cost (USD), cache-hit count/rate over a time range, plus
  breakdowns by model, API key (`key_id` only today), and prompt name.
- **Timeseries** (`GET /dashboard/api/usage/timeseries`): request count,
  cache-hit count, cost, bucketed hourly or daily.
- **Eval history** (`GET /dashboard/api/evals`): per-run score, pass/fail,
  model, prompt name/version, timestamp.
- **Prompts** (`GET /dashboard/api/prompts`): registered prompts + active
  version number.
- **Prompt version timeline** (`GET /dashboard/api/prompts/{name}/versions`):
  per-version created-at, created-by, notes, active flag.
- **Not currently exposed but backed by real columns**: `ApiKey.name`
  (currently only `key_id` int is returned in `by_key` breakdown),
  `RequestLog.prompt_tokens` / `RequestLog.completion_tokens` (currently
  only the summed `total_tokens` is returned).
- **Not tracked anywhere**: request errors/failures, "savings" as a
  distinct metric, org-level credit balances, agents.

## Design

### 1. Directory structure & build

New top-level `dashboard/` directory, sibling to `gatekeep/`, `demo/`,
`migrations/`:

```
dashboard/
  package.json
  vite.config.ts
  tsconfig.json
  tailwind.config.ts
  index.html
  src/
    main.tsx
    App.tsx
    api/           # typed fetch client for /dashboard/api/*
    components/
    pages/          # single DashboardPage for now
```

- Vite + React + TypeScript + Tailwind CSS + Recharts.
- `npm run dev` runs Vite's dev server with `/dashboard/api` proxied to
  `http://localhost:8100` (Gatekeep's own port) for hot-reload during local
  development.
- `npm run build` outputs to `dashboard/dist/`.
- `gatekeep/app.py` mounts `dashboard/dist` via `StaticFiles` and serves
  `dashboard/dist/index.html` for `GET /dashboard` (and any other
  non-`/dashboard/api` path under `/dashboard/*`, so client-side asset
  requests resolve correctly). This lives in the same FastAPI app as
  `/dashboard/api/...` - no proxy needed.
- `demo/static/dashboard.html`, `dashboard.css`, `dashboard.js` are deleted.
- `demo/app.py` loses its `/dashboard` page route and all
  `/api/dashboard/*` proxy endpoints (`_proxy_dashboard_get` and the five
  routes built on it) - Gatekeep now serves its own dashboard directly.

### 2. Docker build

`Dockerfile` becomes multi-stage:

```dockerfile
FROM node:20-slim AS frontend-build
WORKDIR /app/dashboard
COPY dashboard/package*.json ./
RUN npm ci
COPY dashboard ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY gatekeep ./gatekeep
RUN pip install --no-cache-dir -e .
COPY migrations ./migrations
COPY alembic.ini ./
COPY --from=frontend-build /app/dashboard/dist ./dashboard/dist
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn gatekeep.app:app --host 0.0.0.0 --port 8000"]
```

No new container/service in `docker-compose.yml` - the existing `gateway`
service image just gains a build stage.

### 3. Auth

Gatekeep's dashboard API endpoints require `require_api_key` (Bearer
token), same as every other client. Since there's no server-side proxy
hiding a key anymore, the dashboard handles this itself:

- On load, check `localStorage` for a stored key. If absent, render a
  minimal key-entry screen ("Enter your Gatekeep API key") instead of the
  dashboard.
- Once entered, store the key in `localStorage` and attach it as
  `Authorization: Bearer <key>` on every `/dashboard/api/...` request.
- A 401 response from any request clears the stored key and re-shows the
  entry screen (handles revoked/invalid keys).
- A small key icon/button in the header lets the user replace or clear the
  stored key manually.

This mirrors how every other Gatekeep client already authenticates - no new
auth subsystem, no session store.

### 4. Page layout (single page, no sidebar)

- **Header**: "Gatekeep" wordmark, left-aligned; key-management affordance,
  right-aligned.
- **Filter bar**: range picker (24h / 7d / 30d), bucket (hour/day), model
  filter - same controls as today, restyled as a horizontal toolbar.
- **Stat row** (4 cards): Requests, Total Cost, Total Tokens (shown as
  Input/Output split once the backend exposes it), Cache Hit Rate. Each
  card: label, big number, small trend/context line. No error or "savings"
  cards - not tracked.
- **Usage-over-time panel**: Recharts composed chart - bars for request
  volume (stacked total vs. cache-hit), line overlay for cost - replacing
  the current hand-rolled SVG bars.
- **Breakdown panels** (3, grid layout): Cost by Model, Cost by API Key
  (showing `key_name`, not raw `key_id`), Cost by Prompt. Data tables with
  a relative-cost-share bar per row.
- **Prompts panel**: prompt picker + version timeline table (version,
  active badge, created at/by, notes).
- **Eval run history panel**: table with pass/fail pill, score, model,
  prompt/version, timestamp, newest first.

### 5. Backend additions

In `gatekeep/api/dashboard.py`:

- `UsageBreakdownRow` (for `by_key` only) gains `label: str` populated from
  `ApiKey.name` via a join, falling back to `#<id>` if the key was deleted
  but logs remain. Requires changing `_breakdown` to accept an optional
  join, or adding a small dedicated query for the key breakdown specifically
  since it's the only one of the three breakdowns needing a join.
- `UsageSummaryResponse` gains `prompt_tokens: int` and
  `completion_tokens: int` alongside the existing `total_tokens`, computed
  in the same totals query in `usage_summary`.

Both are additive (new fields), so no existing consumer breaks.

### 6. Testing

- `tests/test_dashboard.py` (existing backend tests) gets new assertions
  for `key_name` in `by_key` rows and `prompt_tokens`/`completion_tokens`
  in the summary response.
- Frontend: component-level tests are out of scope for the initial build
  (small, mostly-presentational app); manual verification via `npm run
  build` + running the full stack (`docker compose up`) checking the real
  `/dashboard` page against live data is the acceptance bar, consistent
  with this project's practice of testing bug fixes end-to-end as a real
  user would experience them.

## Open questions / risks

- None blocking. The `key_name` breakdown query shape (join vs. separate
  query) is an implementation detail to resolve in the plan, not a design
  fork.
