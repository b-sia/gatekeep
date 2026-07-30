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
