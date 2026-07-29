/** The current dashboard-wide filter selection: time range, chart bucket
 * interval, and an optional model filter. */
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

/** Row of dropdowns for selecting the dashboard's time range, chart
 * interval, and model filter. */
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
