## Task 11: Dashboard success-rate stat tile (frontend)

**Files:**
- Modify: `dashboard/src/api/types.ts`
- Modify: `dashboard/src/components/StatRow.tsx`

**Interfaces:**
- Consumes: `UsageSummaryResponse.failed_count`/`success_rate` (Task 10).
- Produces: nothing consumed elsewhere - this is the leaf UI change.

No frontend test infrastructure exists in this repo (`find dashboard -iname '*.test.*'` returns nothing), so this task is verified via `npm run build` (runs `tsc` then `vite build`) plus a manual visual check, matching how every other `StatRow`/dashboard-panel change in this codebase has shipped.

- [ ] **Step 1: Add the new fields to the TypeScript type**

In `dashboard/src/api/types.ts`, update `UsageSummaryResponse`:

```typescript
export interface UsageSummaryResponse {
  start: string;
  end: string;
  request_count: number;
  total_tokens: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number;
  spend_usd: number;
  savings_usd: number;
  cache_hit_count: number;
  cache_hit_rate: number;
  failed_count: number;
  success_rate: number;
  by_model: UsageBreakdownRow[];
  by_key: UsageBreakdownRow[];
  by_prompt: UsageBreakdownRow[];
}
```

- [ ] **Step 2: Add the stat tile in `StatRow.tsx`**

In `dashboard/src/components/StatRow.tsx`, change the grid column count from 5 to 6 in both the loading-placeholder branch and the populated branch (`lg:grid-cols-5` -> `lg:grid-cols-6`), add `"Success rate"` to the loading-placeholder label list, and add a sixth `StatCard` after the "Cache hit rate" one:

```tsx
export default function StatRow({ summary }: StatRowProps) {
  if (!summary) {
    return (
      <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-6">
        {[
          "Requests",
          "Total cost",
          "Total tokens",
          "Total savings",
          "Cache hit rate",
          "Success rate",
        ].map((label) => (
          <StatCard key={label} label={label} value="-" context="Loading..." />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-4 px-6 py-4 sm:grid-cols-2 lg:grid-cols-6">
      <StatCard
        label="Requests"
        value={summary.request_count.toLocaleString()}
        context={`${summary.cache_hit_count} cache hits`}
      />
      <StatCard label="Total cost" value={formatUsd(summary.cost_usd)} context="Across all models" />
      <StatCard
        label="Total tokens"
        value={formatTokens(summary.total_tokens)}
        context={`${formatTokens(summary.prompt_tokens)} in / ${formatTokens(summary.completion_tokens)} out`}
      />
      <StatCard
        label="Total savings"
        value={formatUsd(summary.savings_usd)}
        context={`${formatUsd(summary.spend_usd)} spent`}
      />
      <StatCard
        label="Cache hit rate"
        value={`${(summary.cache_hit_rate * 100).toFixed(1)}%`}
        context="Of total requests"
      />
      <StatCard
        label="Success rate"
        value={`${(summary.success_rate * 100).toFixed(1)}%`}
        context={`${summary.failed_count} failed`}
      />
    </div>
  );
}
```

- [ ] **Step 3: Type-check and build**

Run: `cd dashboard && npm run build`
Expected: succeeds with no TypeScript errors.

- [ ] **Step 4: Visual check**

Run the dashboard dev server (`cd dashboard && npm run dev`) against a running gatekeep instance with some request history, and confirm the "Success rate" tile renders correctly at both a wide and narrow viewport (the grid drops to 2 then 1 column per the existing responsive classes). If no running instance is available in this environment, note that in the task's completion report rather than skipping the build check.

- [ ] **Step 5: Commit**

```bash
git add dashboard/src/api/types.ts dashboard/src/components/StatRow.tsx
git commit -m "feat(dashboard): add success rate stat tile"
```

---

