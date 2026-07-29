# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild Gatekeep's cost/usage/eval dashboard as a first-party React/TypeScript single-page app served directly by `gatekeep/app.py`, replacing the vanilla-JS page currently proxied through `demo/app.py`.

**Architecture:** A new top-level `dashboard/` directory (Vite + React + TypeScript + Tailwind + Recharts) builds to `dashboard/dist/`, which `gatekeep/app.py` mounts via `StaticFiles` and serves at `GET /dashboard`. The SPA authenticates directly against Gatekeep's existing `/dashboard/api/*` endpoints using a Bearer API key entered by the user and stored in `localStorage` - no server-side proxy, no new auth subsystem. Two small additive backend changes expose data (API key name, prompt/completion token split) that already exists in the DB but wasn't surfaced via the API. The demo app's proxy routes and static dashboard files are deleted.

**Tech Stack:** FastAPI/SQLAlchemy (existing backend, Python 3.12), Vite 5, React 18, TypeScript 5 (strict), Tailwind CSS 3.4, Recharts 2, pytest/pytest-asyncio/httpx (existing backend test stack).

## Global Constraints

- No em dashes in any code, comment, commit message, or UI copy - use a plain dash `-`.
- No sidebar/multi-page navigation - single page only (per spec Non-goals).
- No session/login auth system - Bearer API key via `localStorage` only (per spec Non-goals and Design §3).
- No changes to `demo/static/index.html` or the demo chat UI itself - only removal of the dashboard proxy/page code (per spec Non-goals).
- No credits, agents, notifications, org/project switcher, error-count cards, or budget/spend-cap visualization anywhere in the UI - none of that data exists (per spec Goals/Non-goals).
- Backend changes must be additive only (new response fields) so no existing consumer breaks (per spec Design §5).
- Frontend component-level automated tests are out of scope for this build; `npm run build` passing (type-check + bundle) is the verification bar for each frontend task, with a full manual `docker compose up` check as final acceptance (per spec Design §6).
- TypeScript strict mode; no `any` types in new code.

---

## File Structure

Backend (modified, no new files):
- `gatekeep/api/dashboard.py` - add `label` to `UsageBreakdownRow`, new `_key_breakdown` query, add `prompt_tokens`/`completion_tokens` to `UsageSummaryResponse`.
- `gatekeep/app.py` - mount `dashboard/dist` as static files, serve `dashboard/dist/index.html` for `/dashboard` and any non-`/dashboard/api` path under `/dashboard/*`.
- `tests/test_dashboard.py` - new assertions for `label` and token-split fields.
- `demo/app.py` - remove `dashboard_page`, `_proxy_dashboard_get`, and the five `dashboard_*` proxy routes.
- `demo/static/dashboard.html`, `demo/static/dashboard.css`, `demo/static/dashboard.js` - deleted.
- `Dockerfile` - becomes multi-stage (Node build stage + Python runtime stage).

New frontend directory `dashboard/`:
```
dashboard/
  package.json
  vite.config.ts
  tsconfig.json
  tailwind.config.ts
  postcss.config.js
  index.html
  src/
    main.tsx
    App.tsx
    index.css
    api/
      types.ts        # response/row shapes matching backend Pydantic models
      client.ts        # fetch wrapper, localStorage key mgmt, endpoint functions
    components/
      KeyEntryScreen.tsx
      Header.tsx
      FilterBar.tsx
      StatRow.tsx
      UsageChart.tsx
      BreakdownTable.tsx
      BreakdownPanels.tsx
      PromptsPanel.tsx
      EvalHistoryPanel.tsx
    pages/
      DashboardPage.tsx
```

Each component has one responsibility: `BreakdownTable` is the generic reusable row-renderer; `BreakdownPanels` just lays out three of them (Cost by Model / Key / Prompt). `DashboardPage` owns all data-fetching and filter state; presentational components are given data via props and stay stateless except `PromptsPanel` (owns its own picker selection + version-timeline fetch, since that's local UI state not shared with anything else).

---

### Task 1: Expose API key name in the `by_key` usage breakdown

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `UsageBreakdownRow.label: str | None` (new optional field, populated only for `by_key` rows).
- Produces: `_key_breakdown(session: AsyncSession, filters: list) -> list[UsageBreakdownRow]` (new function).

- [ ] **Step 1: Write the failing test**

Add this assertion inside `test_usage_summary_totals_and_breakdowns` in `tests/test_dashboard.py`, right after the existing `by_key` assertion (after line 151, `assert by_key[str(key_row.id)]["request_count"] == 3`):

```python
    assert by_key[str(key_row.id)]["label"] == "dashboard-test"
```

(`"dashboard-test"` is the name the `raw_key` fixture already gives the `ApiKey` row at `tests/test_dashboard.py:21`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: FAIL with `KeyError: 'label'` (the field doesn't exist in the response yet).

- [ ] **Step 3: Add the `label` field and the joined key-breakdown query**

In `gatekeep/api/dashboard.py`, change `UsageBreakdownRow` (currently lines 38-46):

```python
class UsageBreakdownRow(BaseModel):
    """One row of a cost/usage breakdown, grouped by a single dimension
    (model id, API key id, or prompt name)."""

    key: str
    label: str | None = None
    request_count: int
    total_tokens: int
    cost_usd: float
    cache_hit_count: int
```

Add a new function directly after `_breakdown` (after line 122, before the `usage_summary` route at line 125):

```python
async def _key_breakdown(
    session: AsyncSession, filters: list
) -> list[UsageBreakdownRow]:
    """Run the same aggregate as `_breakdown` grouped by `RequestLog.key_id`,
    but also join `ApiKey` to attach each key's display name as `label`.

    Uses an outer join so requests from a since-deleted API key still show
    up, with `label` falling back to `#<id>`.
    """
    rows = (
        await session.execute(
            select(
                RequestLog.key_id,
                ApiKey.name,
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
                    0,
                ),
            )
            .outerjoin(ApiKey, RequestLog.key_id == ApiKey.id)
            .where(*filters)
            .group_by(RequestLog.key_id, ApiKey.name)
            .order_by(func.sum(RequestLog.cost_usd).desc())
        )
    ).all()
    return [
        UsageBreakdownRow(
            key=str(key_id),
            label=name if name is not None else f"#{key_id}",
            request_count=count,
            total_tokens=int(total_tokens),
            cost_usd=float(cost_usd),
            cache_hit_count=int(cache_hits),
        )
        for key_id, name, count, total_tokens, cost_usd, cache_hits in rows
    ]
```

In `usage_summary` (currently line 168), change:

```python
    by_key = await _breakdown(session, RequestLog.key_id, filters)
```

to:

```python
    by_key = await _key_breakdown(session, filters)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: PASS

- [ ] **Step 5: Run the full dashboard test module to check for regressions**

Run: `pytest tests/test_dashboard.py -v`
Expected: all tests PASS (the `key` field's value and meaning are unchanged, so no other assertion should break).

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): expose API key name as label in by_key breakdown"
```

---

### Task 2: Expose prompt/completion token split in the usage summary

**Files:**
- Modify: `gatekeep/api/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Produces: `UsageSummaryResponse.prompt_tokens: int`, `UsageSummaryResponse.completion_tokens: int` (new fields).

- [ ] **Step 1: Write the failing test**

Add these assertions inside `test_usage_summary_totals_and_breakdowns` in `tests/test_dashboard.py`, right after `assert body["total_tokens"] == 150 + 300 + 15` (currently line 140):

```python
    assert body["prompt_tokens"] == 100 + 200 + 10
    assert body["completion_tokens"] == 50 + 100 + 5
```

(These match the `prompt_tokens`/`completion_tokens` values passed to the three `_seed_log` calls earlier in the same test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: FAIL with `KeyError: 'prompt_tokens'`.

- [ ] **Step 3: Add the fields to the response model and totals query**

In `gatekeep/api/dashboard.py`, change `UsageSummaryResponse` (currently lines 49-62):

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
    cache_hit_count: int
    cache_hit_rate: float
    by_model: list[UsageBreakdownRow]
    by_key: list[UsageBreakdownRow]
    by_prompt: list[UsageBreakdownRow]
```

In `usage_summary`, change the totals query (currently lines 149-165) to also sum `prompt_tokens`/`completion_tokens`:

```python
    totals_row = (
        await session.execute(
            select(
                func.count(RequestLog.id),
                func.coalesce(func.sum(RequestLog.total_tokens), 0),
                func.coalesce(func.sum(RequestLog.prompt_tokens), 0),
                func.coalesce(func.sum(RequestLog.completion_tokens), 0),
                func.coalesce(func.sum(RequestLog.cost_usd), 0.0),
                func.coalesce(
                    func.sum(func.cast(RequestLog.cached, Integer)),
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
        cache_hit_count,
    ) = totals_row
    request_count = int(request_count)
    cache_hit_count = int(cache_hit_count)
    cache_hit_rate = (cache_hit_count / request_count) if request_count else 0.0
```

And update the final `return` (currently lines 171-182) to pass the new fields:

```python
    return UsageSummaryResponse(
        start=start,
        end=end,
        request_count=request_count,
        total_tokens=int(total_tokens),
        prompt_tokens=int(prompt_tokens),
        completion_tokens=int(completion_tokens),
        cost_usd=float(cost_usd),
        cache_hit_count=cache_hit_count,
        cache_hit_rate=cache_hit_rate,
        by_model=by_model,
        by_key=by_key,
        by_prompt=by_prompt,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_dashboard.py::test_usage_summary_totals_and_breakdowns -v`
Expected: PASS

- [ ] **Step 5: Run the full dashboard test module to check for regressions**

Run: `pytest tests/test_dashboard.py -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add gatekeep/api/dashboard.py tests/test_dashboard.py
git commit -m "feat(dashboard): expose prompt/completion token split in usage summary"
```

---

### Task 3: Scaffold the `dashboard/` Vite + React + TypeScript + Tailwind project

**Files:**
- Create: `dashboard/package.json`
- Create: `dashboard/vite.config.ts`
- Create: `dashboard/tsconfig.json`
- Create: `dashboard/tailwind.config.ts`
- Create: `dashboard/postcss.config.js`
- Create: `dashboard/index.html`
- Create: `dashboard/src/main.tsx`
- Create: `dashboard/src/App.tsx`
- Create: `dashboard/src/index.css`
- Create: `dashboard/.gitignore`

**Interfaces:**
- Produces: a buildable empty shell (`npm run build` succeeds) that later tasks add components into. `App.tsx` exports a default component that later tasks (Task 5, Task 12) will replace the body of.

- [ ] **Step 1: Create `dashboard/package.json`**

```json
{
  "name": "gatekeep-dashboard",
  "private": true,
  "version": "0.1.0",
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "recharts": "^2.12.7"
  },
  "devDependencies": {
    "@types/react": "^18.3.3",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.1",
    "autoprefixer": "^10.4.19",
    "postcss": "^8.4.39",
    "tailwindcss": "^3.4.4",
    "typescript": "^5.5.3",
    "vite": "^5.3.1"
  }
}
```

- [ ] **Step 2: Create `dashboard/vite.config.ts`**

```typescript
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "/dashboard/",
  server: {
    proxy: {
      "/dashboard/api": "http://localhost:8100",
    },
  },
  build: {
    outDir: "dist",
  },
});
```

- [ ] **Step 3: Create `dashboard/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true
  },
  "include": ["src", "vite.config.ts"]
}
```

- [ ] **Step 4: Create `dashboard/tailwind.config.ts`**

```typescript
import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {},
  },
  plugins: [],
} satisfies Config;
```

- [ ] **Step 5: Create `dashboard/postcss.config.js`**

```javascript
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
```

- [ ] **Step 6: Create `dashboard/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Gatekeep Dashboard</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 7: Create `dashboard/src/index.css`**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;
```

- [ ] **Step 8: Create `dashboard/src/App.tsx`** (placeholder, replaced fully in Task 5 and Task 12)

```tsx
export default function App() {
  return <div className="min-h-screen bg-slate-950 text-slate-100">Gatekeep Dashboard</div>;
}
```

- [ ] **Step 9: Create `dashboard/src/main.tsx`**

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 10: Create `dashboard/.gitignore`**

```
node_modules
dist
*.local
```

- [ ] **Step 11: Install dependencies and verify the build**

Run: `cd dashboard && npm install && npm run build`
Expected: exits 0, produces `dashboard/dist/index.html` and `dashboard/dist/assets/*`.

- [ ] **Step 12: Commit**

```bash
git add dashboard/package.json dashboard/package-lock.json dashboard/vite.config.ts dashboard/tsconfig.json dashboard/tailwind.config.ts dashboard/postcss.config.js dashboard/index.html dashboard/src/main.tsx dashboard/src/App.tsx dashboard/src/index.css dashboard/.gitignore
git commit -m "feat(dashboard): scaffold Vite/React/TypeScript/Tailwind project"
```

---

### Task 4: API types and typed fetch client

**Files:**
- Create: `dashboard/src/api/types.ts`
- Create: `dashboard/src/api/client.ts`

**Interfaces:**
- Consumes: nothing (pure data layer, talks to `/dashboard/api/*` from Task 1/2's backend shapes).
- Produces: types `UsageBreakdownRow`, `UsageSummaryResponse`, `TimeseriesBucket`, `TimeseriesResponse`, `EvalRunOut`, `EvalHistoryResponse`, `PromptOut`, `PromptListResponse`, `PromptVersionOut`, `PromptVersionTimelineResponse` (exported from `types.ts`); functions `getStoredApiKey(): string | null`, `setStoredApiKey(key: string): void`, `clearStoredApiKey(): void`, class `UnauthorizedError`, and endpoint functions `getUsageSummary`, `getUsageTimeseries`, `getEvalHistory`, `getPrompts`, `getPromptVersions` (exported from `client.ts`). All later components import from these two files.

- [ ] **Step 1: Create `dashboard/src/api/types.ts`**

```typescript
export interface UsageBreakdownRow {
  key: string;
  label?: string | null;
  request_count: number;
  total_tokens: number;
  cost_usd: number;
  cache_hit_count: number;
}

export interface UsageSummaryResponse {
  start: string;
  end: string;
  request_count: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  cache_hit_count: number;
  cache_hit_rate: number;
  by_model: UsageBreakdownRow[];
  by_key: UsageBreakdownRow[];
  by_prompt: UsageBreakdownRow[];
}

export interface TimeseriesBucket {
  bucket_start: string;
  request_count: number;
  cache_hit_count: number;
  cost_usd: number;
}

export interface TimeseriesResponse {
  start: string;
  end: string;
  interval: string;
  buckets: TimeseriesBucket[];
}

export interface EvalRunOut {
  id: number;
  suite_id: number;
  prompt_name: string;
  prompt_version_id: number;
  version_num: number;
  model: string;
  score: number;
  passed: boolean;
  created_at: string;
}

export interface EvalHistoryResponse {
  runs: EvalRunOut[];
}

export interface PromptOut {
  name: string;
  active_version_num: number | null;
  created_at: string;
  updated_at: string;
}

export interface PromptListResponse {
  prompts: PromptOut[];
}

export interface PromptVersionOut {
  version_num: number;
  active: boolean;
  created_at: string;
  created_by: string | null;
  notes: string | null;
}

export interface PromptVersionTimelineResponse {
  name: string;
  versions: PromptVersionOut[];
}
```

- [ ] **Step 2: Create `dashboard/src/api/client.ts`**

```typescript
import type {
  EvalHistoryResponse,
  PromptListResponse,
  PromptVersionTimelineResponse,
  TimeseriesResponse,
  UsageSummaryResponse,
} from "./types";

const STORAGE_KEY = "gatekeep_dashboard_api_key";

export function getStoredApiKey(): string | null {
  return localStorage.getItem(STORAGE_KEY);
}

export function setStoredApiKey(key: string): void {
  localStorage.setItem(STORAGE_KEY, key);
}

export function clearStoredApiKey(): void {
  localStorage.removeItem(STORAGE_KEY);
}

export class UnauthorizedError extends Error {}

async function request<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
): Promise<T> {
  const apiKey = getStoredApiKey();
  if (!apiKey) {
    throw new UnauthorizedError("No API key stored");
  }
  const url = new URL(`/dashboard/api/${path}`, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url.toString(), {
    headers: { Authorization: `Bearer ${apiKey}` },
  });
  if (response.status === 401) {
    clearStoredApiKey();
    throw new UnauthorizedError("API key was rejected");
  }
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export interface UsageFilters {
  start?: string;
  end?: string;
  model?: string;
  keyId?: number;
  promptName?: string;
}

export function getUsageSummary(filters: UsageFilters): Promise<UsageSummaryResponse> {
  return request<UsageSummaryResponse>("usage/summary", {
    start: filters.start,
    end: filters.end,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

export function getUsageTimeseries(
  filters: UsageFilters & { interval: "hour" | "day" },
): Promise<TimeseriesResponse> {
  return request<TimeseriesResponse>("usage/timeseries", {
    start: filters.start,
    end: filters.end,
    interval: filters.interval,
    model: filters.model,
    key_id: filters.keyId,
    prompt_name: filters.promptName,
  });
}

export function getEvalHistory(promptName?: string): Promise<EvalHistoryResponse> {
  return request<EvalHistoryResponse>("evals", { prompt_name: promptName });
}

export function getPrompts(): Promise<PromptListResponse> {
  return request<PromptListResponse>("prompts");
}

export function getPromptVersions(name: string): Promise<PromptVersionTimelineResponse> {
  return request<PromptVersionTimelineResponse>(`prompts/${encodeURIComponent(name)}/versions`);
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0 (type-checks cleanly; nothing imports these files yet, but `tsc` will still check them since they're under `src/`).

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/api/types.ts dashboard/src/api/client.ts
git commit -m "feat(dashboard): add typed API client and response types"
```

---

### Task 5: Key-entry auth gate

**Files:**
- Create: `dashboard/src/components/KeyEntryScreen.tsx`
- Modify: `dashboard/src/App.tsx`

**Interfaces:**
- Consumes: `getStoredApiKey`, `setStoredApiKey`, `clearStoredApiKey` from `../api/client` (Task 4).
- Produces: `KeyEntryScreen` component with props `{ onKeySaved: () => void }`. `App.tsx` now gates rendering on whether a key is stored; Task 12 will replace the `<DashboardPage>` placeholder usage with the real one once it exists.

- [ ] **Step 1: Create `dashboard/src/components/KeyEntryScreen.tsx`**

```tsx
import { useState, type FormEvent } from "react";
import { setStoredApiKey } from "../api/client";

interface KeyEntryScreenProps {
  onKeySaved: () => void;
}

export default function KeyEntryScreen({ onKeySaved }: KeyEntryScreenProps) {
  const [value, setValue] = useState("");

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    setStoredApiKey(trimmed);
    onKeySaved();
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-6 shadow-xl"
      >
        <h1 className="mb-1 text-lg font-semibold text-slate-100">Gatekeep</h1>
        <p className="mb-4 text-sm text-slate-400">
          Enter your Gatekeep API key to view the dashboard.
        </p>
        <input
          type="password"
          autoFocus
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="sk-..."
          className="mb-4 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 focus:border-indigo-500 focus:outline-none"
        />
        <button
          type="submit"
          className="w-full rounded bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500"
        >
          Continue
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 2: Wire the auth gate into `dashboard/src/App.tsx`**

Replace the full contents of `dashboard/src/App.tsx`:

```tsx
import { useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import { clearStoredApiKey, getStoredApiKey } from "./api/client";

export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);

  function handleUnauthorized() {
    clearStoredApiKey();
    setHasKey(false);
  }

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      Authenticated. Dashboard page wired in Task 12.
      <button onClick={handleUnauthorized} className="ml-2 underline">
        Clear key
      </button>
    </div>
  );
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 4: Manual check**

Run: `cd dashboard && npm run dev`, open the dev server URL. Confirm the key-entry form renders when no key is stored, submitting a value stores it in `localStorage` (`gatekeep_dashboard_api_key`) and switches to the "Authenticated" placeholder, and "Clear key" returns to the entry form.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/components/KeyEntryScreen.tsx dashboard/src/App.tsx
git commit -m "feat(dashboard): add API key entry screen and auth gate"
```

---

### Task 6: Header and filter bar

**Files:**
- Create: `dashboard/src/components/Header.tsx`
- Create: `dashboard/src/components/FilterBar.tsx`

**Interfaces:**
- Produces: `Header` component with props `{ onClearKey: () => void }`. `FilterBar` component with props `{ filters: DashboardFilters; availableModels: string[]; onChange: (filters: DashboardFilters) => void }`, and exported type `DashboardFilters = { rangeDays: 1 | 7 | 30; interval: "hour" | "day"; model: string | null }`. Task 12 imports both and `DashboardFilters` into `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/Header.tsx`**

```tsx
interface HeaderProps {
  onClearKey: () => void;
}

export default function Header({ onClearKey }: HeaderProps) {
  return (
    <header className="flex items-center justify-between border-b border-slate-800 px-6 py-4">
      <span className="text-lg font-semibold tracking-tight text-slate-100">Gatekeep</span>
      <button
        onClick={onClearKey}
        title="Replace or clear stored API key"
        className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800"
      >
        API key
      </button>
    </header>
  );
}
```

- [ ] **Step 2: Create `dashboard/src/components/FilterBar.tsx`**

```tsx
export interface DashboardFilters {
  rangeDays: 1 | 7 | 30;
  interval: "hour" | "day";
  model: string | null;
}

interface FilterBarProps {
  filters: DashboardFilters;
  availableModels: string[];
  onChange: (filters: DashboardFilters) => void;
}

export default function FilterBar({ filters, availableModels, onChange }: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-slate-800 px-6 py-3">
      <select
        value={filters.rangeDays}
        onChange={(event) =>
          onChange({ ...filters, rangeDays: Number(event.target.value) as 1 | 7 | 30 })
        }
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        <option value={1}>Last 24h</option>
        <option value={7}>Last 7d</option>
        <option value={30}>Last 30d</option>
      </select>
      <select
        value={filters.interval}
        onChange={(event) =>
          onChange({ ...filters, interval: event.target.value as "hour" | "day" })
        }
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        <option value="hour">Hourly</option>
        <option value="day">Daily</option>
      </select>
      <select
        value={filters.model ?? ""}
        onChange={(event) => onChange({ ...filters, model: event.target.value || null })}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-200"
      >
        <option value="">All models</option>
        {availableModels.map((model) => (
          <option key={model} value={model}>
            {model}
          </option>
        ))}
      </select>
    </div>
  );
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/Header.tsx dashboard/src/components/FilterBar.tsx
git commit -m "feat(dashboard): add header and filter bar components"
```

---

### Task 7: Stat row (4 summary cards)

**Files:**
- Create: `dashboard/src/components/StatRow.tsx`

**Interfaces:**
- Consumes: `UsageSummaryResponse` from `../api/types` (Task 4).
- Produces: `StatRow` component with props `{ summary: UsageSummaryResponse | null }`. Task 12 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/StatRow.tsx`**

```tsx
import type { UsageSummaryResponse } from "../api/types";

interface StatRowProps {
  summary: UsageSummaryResponse | null;
}

function formatCost(usd: number): string {
  return `$${usd.toFixed(2)}`;
}

function formatTokens(count: number): string {
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}k`;
  return String(count);
}

function StatCard({ label, value, context }: { label: string; value: string; context: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-100">{value}</div>
      <div className="mt-1 text-xs text-slate-500">{context}</div>
    </div>
  );
}

export default function StatRow({ summary }: StatRowProps) {
  if (!summary) {
    return (
      <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-4">
        {["Requests", "Total cost", "Total tokens", "Cache hit rate"].map((label) => (
          <StatCard key={label} label={label} value="-" context="Loading..." />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-4">
      <StatCard
        label="Requests"
        value={summary.request_count.toLocaleString()}
        context={`${summary.cache_hit_count} cache hits`}
      />
      <StatCard label="Total cost" value={formatCost(summary.cost_usd)} context="Across all models" />
      <StatCard
        label="Total tokens"
        value={formatTokens(summary.total_tokens)}
        context={`${formatTokens(summary.prompt_tokens)} in / ${formatTokens(summary.completion_tokens)} out`}
      />
      <StatCard
        label="Cache hit rate"
        value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`}
        context="Of total requests"
      />
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/StatRow.tsx
git commit -m "feat(dashboard): add stat row summary cards"
```

---

### Task 8: Usage-over-time chart

**Files:**
- Create: `dashboard/src/components/UsageChart.tsx`

**Interfaces:**
- Consumes: `TimeseriesResponse` from `../api/types` (Task 4), `recharts` (installed in Task 3).
- Produces: `UsageChart` component with props `{ timeseries: TimeseriesResponse | null }`. Task 12 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/UsageChart.tsx`**

```tsx
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TimeseriesResponse } from "../api/types";

interface UsageChartProps {
  timeseries: TimeseriesResponse | null;
}

export default function UsageChart({ timeseries }: UsageChartProps) {
  const data =
    timeseries?.buckets.map((bucket) => ({
      time: new Date(bucket.bucket_start).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: timeseries.interval === "hour" ? "numeric" : undefined,
      }),
      nonCached: bucket.request_count - bucket.cache_hit_count,
      cached: bucket.cache_hit_count,
      cost: bucket.cost_usd,
    })) ?? [];

  return (
    <div className="mx-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Usage over time</h2>
      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
            <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis yAxisId="left" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <YAxis yAxisId="right" orientation="right" tick={{ fill: "#94a3b8", fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: "#0f172a", border: "1px solid #1e293b", fontSize: 12 }}
            />
            <Bar yAxisId="left" dataKey="nonCached" stackId="requests" fill="#6366f1" name="Requests" />
            <Bar yAxisId="left" dataKey="cached" stackId="requests" fill="#22d3ee" name="Cache hits" />
            <Line
              yAxisId="right"
              type="monotone"
              dataKey="cost"
              stroke="#f59e0b"
              name="Cost (USD)"
              strokeWidth={2}
              dot={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/UsageChart.tsx
git commit -m "feat(dashboard): add usage-over-time composed chart"
```

---

### Task 9: Breakdown tables (Cost by Model / Key / Prompt)

**Files:**
- Create: `dashboard/src/components/BreakdownTable.tsx`
- Create: `dashboard/src/components/BreakdownPanels.tsx`

**Interfaces:**
- Consumes: `UsageBreakdownRow`, `UsageSummaryResponse` from `../api/types` (Task 4).
- Produces: `BreakdownTable` component with props `{ title: string; rows: UsageBreakdownRow[] }`. `BreakdownPanels` component with props `{ summary: UsageSummaryResponse | null }`. Task 12 renders `BreakdownPanels` in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/BreakdownTable.tsx`**

```tsx
import type { UsageBreakdownRow } from "../api/types";

interface BreakdownTableProps {
  title: string;
  rows: UsageBreakdownRow[];
}

export default function BreakdownTable({ title, rows }: BreakdownTableProps) {
  const maxCost = Math.max(1e-9, ...rows.map((row) => row.cost_usd));

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">{title}</h2>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Name</th>
            <th className="pb-2 text-right">Requests</th>
            <th className="pb-2 text-right">Tokens</th>
            <th className="pb-2 text-right">Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-t border-slate-800">
              <td className="py-2 pr-2">
                <div className="text-slate-200">{row.label ?? row.key}</div>
                <div className="h-1 w-full rounded bg-slate-800">
                  <div
                    className="h-1 rounded bg-indigo-500"
                    style={{ width: `${(row.cost_usd / maxCost) * 100}%` }}
                  />
                </div>
              </td>
              <td className="py-2 text-right text-slate-300">{row.request_count}</td>
              <td className="py-2 text-right text-slate-300">{row.total_tokens.toLocaleString()}</td>
              <td className="py-2 text-right text-slate-300">${row.cost_usd.toFixed(2)}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={4} className="py-4 text-center text-slate-500">
                No data for this range.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Create `dashboard/src/components/BreakdownPanels.tsx`**

```tsx
import BreakdownTable from "./BreakdownTable";
import type { UsageSummaryResponse } from "../api/types";

interface BreakdownPanelsProps {
  summary: UsageSummaryResponse | null;
}

export default function BreakdownPanels({ summary }: BreakdownPanelsProps) {
  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 lg:grid-cols-3">
      <BreakdownTable title="Cost by model" rows={summary?.by_model ?? []} />
      <BreakdownTable title="Cost by API key" rows={summary?.by_key ?? []} />
      <BreakdownTable title="Cost by prompt" rows={summary?.by_prompt ?? []} />
    </div>
  );
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 4: Commit**

```bash
git add dashboard/src/components/BreakdownTable.tsx dashboard/src/components/BreakdownPanels.tsx
git commit -m "feat(dashboard): add cost breakdown tables"
```

---

### Task 10: Prompts panel (picker + version timeline)

**Files:**
- Create: `dashboard/src/components/PromptsPanel.tsx`

**Interfaces:**
- Consumes: `getPromptVersions` from `../api/client` (Task 4), `PromptOut`, `PromptVersionOut` from `../api/types` (Task 4).
- Produces: `PromptsPanel` component with props `{ prompts: PromptOut[] }`. Task 12 renders this in `DashboardPage.tsx`, passing `prompts` from `getPrompts()`.

- [ ] **Step 1: Create `dashboard/src/components/PromptsPanel.tsx`**

```tsx
import { useEffect, useState } from "react";
import { getPromptVersions } from "../api/client";
import type { PromptOut, PromptVersionOut } from "../api/types";

interface PromptsPanelProps {
  prompts: PromptOut[];
}

export default function PromptsPanel({ prompts }: PromptsPanelProps) {
  const [selected, setSelected] = useState<string>("");
  const [versions, setVersions] = useState<PromptVersionOut[]>([]);

  useEffect(() => {
    if (!selected && prompts.length > 0) {
      setSelected(prompts[0].name);
    }
  }, [prompts, selected]);

  useEffect(() => {
    if (!selected) {
      setVersions([]);
      return;
    }
    let cancelled = false;
    getPromptVersions(selected).then((res) => {
      if (!cancelled) setVersions(res.versions);
    });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <div className="mx-6 mb-4 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium text-slate-300">Prompts</h2>
        <select
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="rounded border border-slate-700 bg-slate-950 px-2 py-1 text-sm text-slate-200"
        >
          {prompts.map((prompt) => (
            <option key={prompt.name} value={prompt.name}>
              {prompt.name}
            </option>
          ))}
        </select>
      </div>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">Version</th>
            <th className="pb-2">Status</th>
            <th className="pb-2">Created</th>
            <th className="pb-2">Created by</th>
            <th className="pb-2">Notes</th>
          </tr>
        </thead>
        <tbody>
          {versions.map((version) => (
            <tr key={version.version_num} className="border-t border-slate-800">
              <td className="py-2 text-slate-200">v{version.version_num}</td>
              <td className="py-2">
                {version.active && (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Active
                  </span>
                )}
              </td>
              <td className="py-2 text-slate-300">{new Date(version.created_at).toLocaleString()}</td>
              <td className="py-2 text-slate-300">{version.created_by ?? "-"}</td>
              <td className="py-2 text-slate-400">{version.notes ?? "-"}</td>
            </tr>
          ))}
          {versions.length === 0 && (
            <tr>
              <td colSpan={5} className="py-4 text-center text-slate-500">
                {selected ? "No versions yet." : "No prompts registered."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/PromptsPanel.tsx
git commit -m "feat(dashboard): add prompts panel with version timeline"
```

---

### Task 11: Eval run history panel

**Files:**
- Create: `dashboard/src/components/EvalHistoryPanel.tsx`

**Interfaces:**
- Consumes: `EvalRunOut` from `../api/types` (Task 4).
- Produces: `EvalHistoryPanel` component with props `{ runs: EvalRunOut[] }`. Task 12 renders this in `DashboardPage.tsx`.

- [ ] **Step 1: Create `dashboard/src/components/EvalHistoryPanel.tsx`**

```tsx
import type { EvalRunOut } from "../api/types";

interface EvalHistoryPanelProps {
  runs: EvalRunOut[];
}

export default function EvalHistoryPanel({ runs }: EvalHistoryPanelProps) {
  return (
    <div className="mx-6 mb-6 rounded-lg border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-medium text-slate-300">Eval run history</h2>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            <th className="pb-2">When</th>
            <th className="pb-2">Prompt</th>
            <th className="pb-2">Version</th>
            <th className="pb-2">Model</th>
            <th className="pb-2 text-right">Score</th>
            <th className="pb-2">Result</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-t border-slate-800">
              <td className="py-2 text-slate-300">{new Date(run.created_at).toLocaleString()}</td>
              <td className="py-2 text-slate-200">{run.prompt_name}</td>
              <td className="py-2 text-slate-300">v{run.version_num}</td>
              <td className="py-2 text-slate-300">{run.model}</td>
              <td className="py-2 text-right text-slate-300">{run.score.toFixed(2)}</td>
              <td className="py-2">
                {run.passed ? (
                  <span className="rounded bg-emerald-900 px-2 py-0.5 text-xs text-emerald-300">
                    Pass
                  </span>
                ) : (
                  <span className="rounded bg-red-900 px-2 py-0.5 text-xs text-red-300">Fail</span>
                )}
              </td>
            </tr>
          ))}
          {runs.length === 0 && (
            <tr>
              <td colSpan={6} className="py-4 text-center text-slate-500">
                No eval runs yet.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
```

- [ ] **Step 2: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 3: Commit**

```bash
git add dashboard/src/components/EvalHistoryPanel.tsx
git commit -m "feat(dashboard): add eval run history panel"
```

---

### Task 12: Assemble DashboardPage and wire it into App

**Files:**
- Create: `dashboard/src/pages/DashboardPage.tsx`
- Modify: `dashboard/src/App.tsx`

**Interfaces:**
- Consumes: `Header` (Task 6), `FilterBar`/`DashboardFilters` (Task 6), `StatRow` (Task 7), `UsageChart` (Task 8), `BreakdownPanels` (Task 9), `PromptsPanel` (Task 10), `EvalHistoryPanel` (Task 11), `getUsageSummary`/`getUsageTimeseries`/`getEvalHistory`/`getPrompts`/`UnauthorizedError` from `../api/client` (Task 4).
- Produces: `DashboardPage` component with props `{ onUnauthorized: () => void }`. This is the final `App.tsx` integration point - no later task depends on this one.

- [ ] **Step 1: Create `dashboard/src/pages/DashboardPage.tsx`**

```tsx
import { useCallback, useEffect, useState } from "react";
import Header from "../components/Header";
import FilterBar, { type DashboardFilters } from "../components/FilterBar";
import StatRow from "../components/StatRow";
import UsageChart from "../components/UsageChart";
import BreakdownPanels from "../components/BreakdownPanels";
import PromptsPanel from "../components/PromptsPanel";
import EvalHistoryPanel from "../components/EvalHistoryPanel";
import {
  UnauthorizedError,
  getEvalHistory,
  getPrompts,
  getUsageSummary,
  getUsageTimeseries,
} from "../api/client";
import type { EvalRunOut, PromptOut, TimeseriesResponse, UsageSummaryResponse } from "../api/types";

interface DashboardPageProps {
  onUnauthorized: () => void;
}

export default function DashboardPage({ onUnauthorized }: DashboardPageProps) {
  const [filters, setFilters] = useState<DashboardFilters>({
    rangeDays: 7,
    interval: "day",
    model: null,
  });
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [timeseries, setTimeseries] = useState<TimeseriesResponse | null>(null);
  const [runs, setRuns] = useState<EvalRunOut[]>([]);
  const [prompts, setPrompts] = useState<PromptOut[]>([]);

  const load = useCallback(async () => {
    const end = new Date();
    const start = new Date(end.getTime() - filters.rangeDays * 24 * 60 * 60 * 1000);
    const windowParams = { start: start.toISOString(), end: end.toISOString() };
    try {
      const [summaryRes, timeseriesRes, evalsRes, promptsRes] = await Promise.all([
        getUsageSummary({ ...windowParams, model: filters.model ?? undefined }),
        getUsageTimeseries({
          ...windowParams,
          interval: filters.interval,
          model: filters.model ?? undefined,
        }),
        getEvalHistory(),
        getPrompts(),
      ]);
      setSummary(summaryRes);
      setTimeseries(timeseriesRes);
      setRuns(evalsRes.runs);
      setPrompts(promptsRes.prompts);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      throw err;
    }
  }, [filters, onUnauthorized]);

  useEffect(() => {
    load();
  }, [load]);

  const availableModels = summary ? summary.by_model.map((row) => row.key) : [];

  return (
    <div className="min-h-screen bg-slate-950">
      <Header onClearKey={onUnauthorized} />
      <FilterBar filters={filters} availableModels={availableModels} onChange={setFilters} />
      <StatRow summary={summary} />
      <UsageChart timeseries={timeseries} />
      <BreakdownPanels summary={summary} />
      <PromptsPanel prompts={prompts} />
      <EvalHistoryPanel runs={runs} />
    </div>
  );
}
```

- [ ] **Step 2: Replace `dashboard/src/App.tsx` to use the real `DashboardPage`**

```tsx
import { useState } from "react";
import KeyEntryScreen from "./components/KeyEntryScreen";
import DashboardPage from "./pages/DashboardPage";
import { clearStoredApiKey, getStoredApiKey } from "./api/client";

export default function App() {
  const [hasKey, setHasKey] = useState<boolean>(() => getStoredApiKey() !== null);

  function handleUnauthorized() {
    clearStoredApiKey();
    setHasKey(false);
  }

  if (!hasKey) {
    return <KeyEntryScreen onKeySaved={() => setHasKey(true)} />;
  }

  return <DashboardPage onUnauthorized={handleUnauthorized} />;
}
```

- [ ] **Step 3: Verify the build**

Run: `cd dashboard && npm run build`
Expected: exits 0.

- [ ] **Step 4: Manual check against a running backend**

With Gatekeep running locally on port 8100 (`docker compose up postgres redis gateway` or `uvicorn gatekeep.app:app --port 8100`) and an API key created, run `cd dashboard && npm run dev`, open the dev server URL, enter the key, and confirm: stat cards populate, the usage chart renders bars/line, all three breakdown tables show rows, the prompt picker lists prompts and shows a version timeline on selection, and the eval history table lists runs (empty states render cleanly if there's no data yet).

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/pages/DashboardPage.tsx dashboard/src/App.tsx
git commit -m "feat(dashboard): assemble dashboard page and wire into app"
```

---

### Task 13: Serve the built dashboard from `gatekeep/app.py`

**Files:**
- Modify: `gatekeep/app.py`

**Interfaces:**
- Consumes: `dashboard/dist/` (built output from Task 3-12).
- Produces: `GET /dashboard` and any non-`/dashboard/api` path under `/dashboard/*` served from `dashboard/dist/index.html`; static assets under `/dashboard/assets/*` served from `dashboard/dist/assets/*`.

- [ ] **Step 1: Build the dashboard so there's something to serve**

Run: `cd dashboard && npm run build`
Expected: `dashboard/dist/index.html` exists.

- [ ] **Step 2: Add the static mount and SPA fallback route to `gatekeep/app.py`**

Add these imports near the top of `gatekeep/app.py`, alongside the existing `fastapi` imports (after line 15, `from fastapi.responses import JSONResponse, Response, StreamingResponse`):

```python
import pathlib

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
```

Add this constant near the top-level `app = FastAPI(...)` line (currently line 92), directly after it:

```python
app = FastAPI(title="gatekeep")
app.include_router(dashboard_router)

_DASHBOARD_DIST = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "dist"

if (_DASHBOARD_DIST / "assets").is_dir():
    app.mount(
        "/dashboard/assets",
        StaticFiles(directory=str(_DASHBOARD_DIST / "assets")),
        name="dashboard-assets",
    )


@app.get("/dashboard")
@app.get("/dashboard/{path:path}")
async def serve_dashboard(path: str = "") -> FileResponse:
    """Serve the dashboard SPA's index.html for any non-API path under
    `/dashboard`, so client-side routing/asset requests resolve correctly.

    Registered after `dashboard_router` (which owns `/dashboard/api/*`), so
    FastAPI matches the more specific API routes first.
    """
    return FileResponse(_DASHBOARD_DIST / "index.html")
```

Note: this route must be defined after `app.include_router(dashboard_router)` so the router's `/dashboard/api/*` routes (which are more specific) take precedence over the catch-all `/dashboard/{path:path}` - FastAPI matches routes in registration order, and `dashboard_router` is already included on line 93 before this new code.

- [ ] **Step 3: Verify existing backend tests still pass**

Run: `pytest tests/ -v`
Expected: all tests PASS (this change only adds routes, doesn't touch `/dashboard/api/*` behavior).

- [ ] **Step 4: Manual check**

Run: `uvicorn gatekeep.app:app --port 8100` (with `dashboard/dist` built) and open `http://localhost:8100/dashboard` in a browser. Confirm the SPA loads (key-entry screen appears), and that `http://localhost:8100/dashboard/anything` also serves the same `index.html` rather than 404ing.

- [ ] **Step 5: Commit**

```bash
git add gatekeep/app.py
git commit -m "feat(dashboard): serve built dashboard SPA from gatekeep/app.py"
```

---

### Task 14: Remove the demo app's dashboard proxy and static files

**Files:**
- Modify: `demo/app.py`
- Delete: `demo/static/dashboard.html`
- Delete: `demo/static/dashboard.css`
- Delete: `demo/static/dashboard.js`

**Interfaces:**
- Consumes: nothing new.
- Produces: nothing consumed by later tasks - this is cleanup.

- [ ] **Step 1: Remove the dashboard route, proxy helper, and five proxy endpoints from `demo/app.py`**

Delete the `/dashboard` page route (currently lines 83-91):

```python
@app.get("/dashboard")
async def dashboard_page():
    """Serve the cost/usage/eval dashboard page."""
    html_path = pathlib.Path(__file__).parent / "static" / "dashboard.html"
    response = FileResponse(html_path, media_type="text/html")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
```

Delete the `_proxy_dashboard_get` helper and the five `dashboard_*` proxy routes built on it (currently lines 94-179, from `async def _proxy_dashboard_get` through the end of `dashboard_prompt_versions`):

```python
async def _proxy_dashboard_get(path: str, params: dict) -> dict:
    """Forward one GET request to a Gatekeep `/dashboard/api/...` endpoint.

    Attaches the demo's configured API_KEY as a Bearer token (kept
    server-side, never exposed to the browser) and forwards query
    parameters verbatim. Raises HTTPException mirroring the upstream
    status code on any non-2xx response or connection failure.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{GATEKEEP_URL}/dashboard/api/{path}",
                params={k: v for k, v in params.items() if v is not None},
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=30.0,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Gateway error: {str(e)}")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/api/dashboard/summary")
async def dashboard_summary(
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    key_id: int | None = None,
    prompt_name: str | None = None,
) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/usage/summary` endpoint."""
    return await _proxy_dashboard_get(
        "usage/summary",
        {
            "start": start,
            "end": end,
            "model": model,
            "key_id": key_id,
            "prompt_name": prompt_name,
        },
    )


@app.get("/api/dashboard/timeseries")
async def dashboard_timeseries(
    start: str | None = None,
    end: str | None = None,
    interval: str = "day",
    model: str | None = None,
    key_id: int | None = None,
    prompt_name: str | None = None,
) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/usage/timeseries` endpoint."""
    return await _proxy_dashboard_get(
        "usage/timeseries",
        {
            "start": start,
            "end": end,
            "interval": interval,
            "model": model,
            "key_id": key_id,
            "prompt_name": prompt_name,
        },
    )


@app.get("/api/dashboard/evals")
async def dashboard_evals(prompt_name: str | None = None, limit: int = 50) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/evals` endpoint."""
    return await _proxy_dashboard_get(
        "evals", {"prompt_name": prompt_name, "limit": limit}
    )


@app.get("/api/dashboard/prompts")
async def dashboard_prompts() -> dict:
    """Proxy for Gatekeep's `/dashboard/api/prompts` endpoint."""
    return await _proxy_dashboard_get("prompts", {})


@app.get("/api/dashboard/prompts/{name}/versions")
async def dashboard_prompt_versions(name: str) -> dict:
    """Proxy for Gatekeep's `/dashboard/api/prompts/{name}/versions` endpoint."""
    return await _proxy_dashboard_get(f"prompts/{name}/versions", {})
```

Everything else in `demo/app.py` (the `/` index route, `Message` model, `NoCacheMiddleware`, `GATEKEEP_URL`/`API_KEY`/`DEFAULT_MODEL` config, `/api/chat`, `/api/chat-sync`, the static mount, and the `__main__` block) stays untouched - it's shared with or specific to the chat demo, not the dashboard.

- [ ] **Step 2: Delete the demo dashboard static files**

```bash
git rm demo/static/dashboard.html demo/static/dashboard.css demo/static/dashboard.js
```

- [ ] **Step 3: Verify the demo app still starts and its remaining routes work**

Run: `python -c "import ast; ast.parse(open('demo/app.py').read())"` to confirm no syntax errors, then `cd demo && python -m uvicorn app:app --port 8200 &` and `curl -s http://localhost:8200/ -o /dev/null -w "%{http_code}\n"` (expect `200`), then stop the server. Confirm `curl -s http://localhost:8200/dashboard -o /dev/null -w "%{http_code}\n"` now returns `404` (route removed).

- [ ] **Step 4: Commit**

```bash
git add demo/app.py demo/static/dashboard.html demo/static/dashboard.css demo/static/dashboard.js
git commit -m "refactor(demo): remove dashboard proxy routes and static files, now served by gatekeep"
```

---

### Task 15: Multi-stage Docker build

**Files:**
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: `dashboard/` (Task 3-12), `gatekeep/app.py`'s static mount (Task 13).
- Produces: a single-image build where `dashboard/dist` is baked into the Python runtime image at `/app/dashboard/dist`, matching the path `gatekeep/app.py`'s `_DASHBOARD_DIST` constant resolves to at runtime.

- [ ] **Step 1: Replace `Dockerfile` with the multi-stage build**

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

- [ ] **Step 2: Verify the image builds**

Run: `docker build -t gatekeep-dashboard-test .`
Expected: exits 0, both stages complete (`npm ci`/`npm run build` in the frontend stage, `pip install`/`COPY --from=frontend-build` in the runtime stage).

- [ ] **Step 3: Verify `dashboard/dist` landed in the right place inside the image**

Run: `docker run --rm gatekeep-dashboard-test ls /app/dashboard/dist`
Expected: lists `index.html` and `assets/` - matches `_DASHBOARD_DIST` in `gatekeep/app.py` (`pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "dist"`, i.e. `/app/dashboard/dist` when `gatekeep/app.py` is at `/app/gatekeep/app.py` inside the image).

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "build: multi-stage Dockerfile to build and bundle the dashboard SPA"
```

---

### Task 16: Full-stack end-to-end verification

**Files:** none (verification only, per spec Design §6 - no frontend component tests, `docker compose up` + manual check is the acceptance bar).

**Interfaces:** none.

- [ ] **Step 1: Run the full backend test suite one more time**

Run: `pytest tests/ -v`
Expected: all tests PASS.

- [ ] **Step 2: Bring up the full stack via Docker Compose**

Run: `docker compose up --build`
Expected: `postgres`, `redis`, `gateway` (and other existing services) start cleanly; the `gateway` service's build now runs the multi-stage Dockerfile from Task 15.

- [ ] **Step 3: Create a real API key and seed some usage data**

Use whatever existing mechanism the repo provides for creating an API key (check `gatekeep/cli.py` or `scripts/` for a key-creation command), then send a few real chat completion requests through `http://localhost:8100/v1/chat/completions` with that key so `request_logs` has rows to display.

- [ ] **Step 4: Open the real `/dashboard` page and verify pixel-by-pixel against the spec**

Open `http://localhost:8100/dashboard`. Verify, obsessively:
- Key-entry screen appears first; entering the key stores it and loads the dashboard.
- Header shows "Gatekeep" left-aligned and the API-key button right-aligned; clicking it clears the key and returns to the entry screen.
- Filter bar has range (24h/7d/30d), interval (hour/day), and model select controls, laid out as a horizontal toolbar.
- Stat row shows exactly 4 cards (Requests, Total Cost, Total Tokens with input/output split, Cache Hit Rate) - no credits/agents/error/savings cards anywhere.
- Usage-over-time panel renders a Recharts composed chart (stacked request/cache-hit bars + cost line), not the old SVG bars.
- Three breakdown panels in a grid show Cost by Model, Cost by API Key (real key names, not raw ids), and Cost by Prompt, each with a relative-cost bar per row.
- Prompts panel's picker switches the version timeline table (version, active badge, created at/by, notes).
- Eval run history table shows pass/fail pills, score, model, prompt/version, timestamp, newest first.
- No sidebar, no login/session UI beyond the API key screen.
- A 401 (e.g. paste an invalid key into `localStorage` and reload) clears the stored key and re-shows the entry screen.

- [ ] **Step 5: Verify the demo app no longer serves a dashboard**

With the demo app also running (`python demo/app.py`, port 8200), confirm `http://localhost:8200/dashboard` returns 404 and the demo chat UI at `http://localhost:8200/` still works normally.

- [ ] **Step 6: Fix anything that looks off**

If any visual or functional issue surfaces during Step 4/5 - even something not directly caused by this plan's changes - fix it before considering this task done, per the project's engineering standard of pixel-perfect UI and zero tolerance for lint/test issues left behind.

No commit for this task - it's verification only. If Step 6 requires a fix, make that fix as its own small commit in the relevant file(s).

---

## Self-Review Notes

- **Spec coverage:** Directory structure/build (Task 3), Docker (Task 15), Auth (Task 5), Page layout - header/filters/stats/chart/breakdowns/prompts/evals (Tasks 6-11), Backend additions (Tasks 1-2), Testing (Tasks 1-2 backend assertions, Task 16 manual e2e) - all spec sections have a task.
- **Placeholder scan:** no TBD/TODO/"add error handling"-style steps; every code step has real code.
- **Type consistency:** `DashboardFilters` defined once in `FilterBar.tsx` (Task 6) and imported by `DashboardPage.tsx` (Task 12); all API response types defined once in `api/types.ts` (Task 4) and imported everywhere else; `UnauthorizedError` defined once in `api/client.ts` and used in both `App.tsx` (indirectly via `DashboardPage`'s `onUnauthorized` prop) and `DashboardPage.tsx` directly.
