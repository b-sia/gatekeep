/** Formats an ISO timestamp as a short, interval-appropriate label for chart
 * X-axes: date only for daily buckets, date+hour for hourly, date+hour+minute
 * for minute buckets. */
export function formatBucketLabel(
  isoString: string,
  interval: "minute" | "hour" | "day",
): string {
  return new Date(isoString).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: interval !== "day" ? "numeric" : undefined,
    minute: interval === "minute" ? "numeric" : undefined,
  });
}

/** Formats a USD amount, using extra decimal places for sub-cent values so
 * distinct small amounts (common with per-request LLM costs) don't collapse
 * into duplicate-looking "$0.00" ticks. */
export function formatUsd(value: number): string {
  if (value !== 0 && Math.abs(value) < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

/** Formats a millisecond duration for display, switching to seconds above
 * 1000ms so multi-second streams stay readable. A null or undefined value
 * means "no samples", rendered as "-" rather than a misleading 0ms. */
export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  if (value >= 10) return `${Math.round(value)}ms`;
  return `${value.toFixed(1)}ms`;
}
