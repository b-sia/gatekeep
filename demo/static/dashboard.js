/**
 * Gatekeep dashboard front-end.
 *
 * Fetches aggregated cost/usage/eval data from this demo app's own
 * `/api/dashboard/*` proxy routes (which attach the server-side API key)
 * and renders it as stat tiles, a plain-SVG bar chart, and plain HTML
 * tables. No external JS dependencies - vanilla fetch + DOM.
 */

const state = {
  rangeDays: 7,
  interval: "day",
  model: "",
  promptName: "",
};

/**
 * Fetch JSON from one of this app's `/api/dashboard/*` routes.
 *
 * @param {string} path - path under /api/dashboard, e.g. "summary".
 * @param {Object} params - query params to append (falsy values dropped).
 * @returns {Promise<Object>} the parsed JSON body.
 * @throws {Error} if the response is not ok; message includes the body text.
 */
async function fetchDashboard(path, params = {}) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v !== null && v !== undefined) query.set(k, v);
  }
  const qs = query.toString();
  const url = `/api/dashboard/${path}${qs ? `?${qs}` : ""}`;
  const resp = await fetch(url);
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.json();
}

/**
 * Format a number as USD currency with 4 decimal places (costs are often
 * fractions of a cent).
 * @param {number} value
 * @returns {string}
 */
function formatUsd(value) {
  return `$${value.toFixed(4)}`;
}

/**
 * Format a fraction (0..1) as a percentage string.
 * @param {number} value
 * @returns {string}
 */
function formatPercent(value) {
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * Show an error message in the page's error banner, or hide the banner if
 * `message` is falsy.
 * @param {string|null} message
 */
function showError(message) {
  const banner = document.getElementById("error-banner");
  if (!message) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  banner.hidden = false;
  banner.textContent = message;
}

/**
 * Compute the ISO-8601 start/end bounds for the currently selected range.
 * @returns {{start: string, end: string}}
 */
function currentWindow() {
  const end = new Date();
  const start = new Date(end.getTime() - state.rangeDays * 24 * 60 * 60 * 1000);
  return { start: start.toISOString(), end: end.toISOString() };
}

/**
 * Render one breakdown table (by_model/by_key/by_prompt rows) into the
 * `<tbody>` of the table with id `tableId`.
 * @param {string} tableId
 * @param {Array<Object>} rows - UsageBreakdownRow-shaped objects.
 */
function renderBreakdownTable(tableId, rows) {
  const tbody = document.querySelector(`#${tableId} tbody`);
  tbody.innerHTML = "";
  if (rows.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No data in range</td></tr>`;
    return;
  }
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.key}</td>
      <td>${row.request_count}</td>
      <td>${row.total_tokens}</td>
      <td>${formatUsd(row.cost_usd)}</td>
      <td>${row.cache_hit_count}</td>
    `;
    tbody.appendChild(tr);
  }
}

/**
 * Render the top-line stat tiles from a UsageSummaryResponse.
 * @param {Object} summary
 */
function renderStats(summary) {
  document.getElementById("stat-requests").textContent = summary.request_count;
  document.getElementById("stat-cost").textContent = formatUsd(summary.cost_usd);
  document.getElementById("stat-tokens").textContent = summary.total_tokens;
  document.getElementById("stat-cache-rate").textContent = formatPercent(
    summary.cache_hit_rate,
  );
}

/**
 * Populate the model-filter <select> with the distinct models seen in
 * `byModel`, preserving the currently selected value if still present.
 * @param {Array<Object>} byModel
 */
function populateModelFilter(byModel) {
  const select = document.getElementById("model-filter");
  const previous = select.value;
  select.innerHTML = '<option value="">All models</option>';
  for (const row of byModel) {
    const option = document.createElement("option");
    option.value = row.key;
    option.textContent = row.key;
    select.appendChild(option);
  }
  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  }
}

/**
 * Render a simple grouped-bar SVG chart of request volume (with a stacked
 * cache-hit segment) over the timeseries buckets.
 * @param {Array<Object>} buckets - TimeseriesBucket-shaped objects.
 */
function renderTimeseriesChart(buckets) {
  const container = document.getElementById("timeseries-chart");
  container.innerHTML = "";
  if (buckets.length === 0) {
    container.innerHTML = "<p>No data in range</p>";
    return;
  }

  const width = Math.max(480, buckets.length * 48);
  const height = 220;
  const padding = { top: 10, right: 10, bottom: 30, left: 10 };
  const plotHeight = height - padding.top - padding.bottom;
  const maxCount = Math.max(...buckets.map((b) => b.request_count), 1);
  const barWidth = (width - padding.left - padding.right) / buckets.length;

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Request volume over time");

  buckets.forEach((bucket, i) => {
    const x = padding.left + i * barWidth + barWidth * 0.15;
    const w = barWidth * 0.7;
    const totalHeight = (bucket.request_count / maxCount) * plotHeight;
    const cachedHeight = (bucket.cache_hit_count / maxCount) * plotHeight;
    const yTotal = padding.top + (plotHeight - totalHeight);
    const yCached = padding.top + (plotHeight - cachedHeight);

    const totalRect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    totalRect.setAttribute("x", x);
    totalRect.setAttribute("y", yTotal);
    totalRect.setAttribute("width", w);
    totalRect.setAttribute("height", Math.max(totalHeight, 0));
    totalRect.setAttribute("class", "chart-bar");
    totalRect.setAttribute(
      "title",
      `${bucket.bucket_start}: ${bucket.request_count} requests, ${formatUsd(bucket.cost_usd)}`,
    );
    svg.appendChild(totalRect);

    if (bucket.cache_hit_count > 0) {
      const cachedRect = document.createElementNS(
        "http://www.w3.org/2000/svg",
        "rect",
      );
      cachedRect.setAttribute("x", x);
      cachedRect.setAttribute("y", yCached);
      cachedRect.setAttribute("width", w);
      cachedRect.setAttribute("height", Math.max(cachedHeight, 0));
      cachedRect.setAttribute("class", "chart-bar cached");
      svg.appendChild(cachedRect);
    }

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", x + w / 2);
    label.setAttribute("y", height - 10);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "chart-axis-label");
    const d = new Date(bucket.bucket_start);
    label.textContent =
      state.interval === "hour"
        ? `${d.getHours()}:00`
        : `${d.getMonth() + 1}/${d.getDate()}`;
    svg.appendChild(label);
  });

  container.appendChild(svg);
}

/**
 * Render the eval-run history table.
 * @param {Array<Object>} runs - EvalRunOut-shaped objects, newest first.
 */
function renderEvalsTable(runs) {
  const tbody = document.querySelector("#table-evals tbody");
  tbody.innerHTML = "";
  if (runs.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="6">No eval runs recorded</td></tr>`;
    return;
  }
  for (const run of runs) {
    const tr = document.createElement("tr");
    const badge = run.passed
      ? '<span class="pass-badge">PASS</span>'
      : '<span class="fail-badge">FAIL</span>';
    tr.innerHTML = `
      <td>${new Date(run.created_at).toLocaleString()}</td>
      <td>${run.prompt_name}</td>
      <td>v${run.version_num}</td>
      <td>${run.model}</td>
      <td>${run.score.toFixed(2)}</td>
      <td>${badge}</td>
    `;
    tbody.appendChild(tr);
  }
}

/**
 * Render the prompt version timeline table.
 * @param {Array<Object>} versions - PromptVersionOut-shaped objects.
 */
function renderPromptVersionsTable(versions) {
  const tbody = document.querySelector("#table-prompt-versions tbody");
  tbody.innerHTML = "";
  if (versions.length === 0) {
    tbody.innerHTML = `<tr class="empty-row"><td colspan="5">Select a prompt to see its version history</td></tr>`;
    return;
  }
  for (const v of versions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>v${v.version_num}</td>
      <td>${v.active ? '<span class="pass-badge">ACTIVE</span>' : ""}</td>
      <td>${new Date(v.created_at).toLocaleString()}</td>
      <td>${v.created_by || ""}</td>
      <td>${v.notes || ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

/**
 * Populate the prompt-picker <select> with the given prompt names,
 * preserving the current selection if still present.
 * @param {Array<Object>} prompts - PromptOut-shaped objects.
 */
function populatePromptPicker(prompts) {
  const select = document.getElementById("prompt-picker");
  const previous = select.value;
  select.innerHTML = '<option value="">All prompts</option>';
  for (const p of prompts) {
    const option = document.createElement("option");
    option.value = p.name;
    option.textContent = `${p.name} (v${p.active_version_num ?? "-"})`;
    select.appendChild(option);
  }
  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  }
}

/**
 * Reload usage summary + timeseries + evals for the current filter state,
 * and refresh the prompt version timeline if a prompt is selected.
 */
async function refreshAll() {
  showError(null);
  const { start, end } = currentWindow();
  try {
    const [summary, timeseries, evals, prompts] = await Promise.all([
      fetchDashboard("summary", { start, end, model: state.model }),
      fetchDashboard("timeseries", {
        start,
        end,
        interval: state.interval,
        model: state.model,
      }),
      fetchDashboard("evals", { prompt_name: state.promptName }),
      fetchDashboard("prompts", {}),
    ]);

    renderStats(summary);
    populateModelFilter(summary.by_model);
    renderBreakdownTable("table-by-model", summary.by_model);
    renderBreakdownTable("table-by-key", summary.by_key);
    renderBreakdownTable("table-by-prompt", summary.by_prompt);
    renderTimeseriesChart(timeseries.buckets);
    renderEvalsTable(evals.runs);
    populatePromptPicker(prompts.prompts);

    if (state.promptName) {
      const versions = await fetchDashboard(
        `prompts/${encodeURIComponent(state.promptName)}/versions`,
      );
      renderPromptVersionsTable(versions.versions);
    } else {
      renderPromptVersionsTable([]);
    }
  } catch (err) {
    showError(`Failed to load dashboard data: ${err.message}`);
  }
}

document.getElementById("range").addEventListener("change", (e) => {
  state.rangeDays = Number(e.target.value);
  refreshAll();
});
document.getElementById("interval").addEventListener("change", (e) => {
  state.interval = e.target.value;
  refreshAll();
});
document.getElementById("model-filter").addEventListener("change", (e) => {
  state.model = e.target.value;
  refreshAll();
});
document.getElementById("prompt-picker").addEventListener("change", (e) => {
  state.promptName = e.target.value;
  refreshAll();
});
document.getElementById("refresh-btn").addEventListener("click", refreshAll);

refreshAll();
