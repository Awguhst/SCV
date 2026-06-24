// Single View of Wealth dashboard frontend.
// Plain JS + Chart.js, talking directly to the FastAPI JSON endpoints below.
// No build step / framework - this is a thin presentation layer over the API.

const GBP = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 0 });
const INT = new Intl.NumberFormat("en-GB");

const CHART_COLORS = {
  primary: "#ffb779",
  primaryDark: "#cd7f32",
  accent: "#e9c176",
  border: "#2c2e30",
  muted: "#a89a8e",
  danger: "#e57373",
};

let charts = {};

function destroyChart(key) {
  if (charts[key]) {
    charts[key].destroy();
    delete charts[key];
  }
}

function showToast(message, isError = false) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className =
    "toast fixed top-20 right-8 z-50 px-4 py-3 rounded-lg text-sm font-medium shadow-lg " +
    (isError ? "bg-red-500/90 text-white" : "bg-card border border-primary-dark text-on-surface");
  toast.style.opacity = "1";
  toast.style.pointerEvents = "auto";
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.pointerEvents = "none";
  }, 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${path} failed (${res.status})`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function switchView(view) {
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  document.getElementById(`view-${view}`).classList.add("active");

  // The profile page is a sub-page of Directory (reached only by clicking a
  // search result), not a top-level nav destination - keep Directory
  // highlighted while it's open instead of clearing the nav highlight.
  const navTarget = view === "profile" ? "directory" : view;
  document.querySelectorAll(".nav-link").forEach((el) => el.classList.remove("active"));
  document.querySelector(`.nav-link[data-view="${navTarget}"]`)?.classList.add("active");

  if (view === "dashboard") loadDashboard();
  if (view === "quality") loadQuality();
}

document.querySelectorAll(".nav-link").forEach((btn) => {
  // Nav buttons with no data-view (e.g. the cosmetic "Settings" entry) are
  // intentionally inert - nothing to switch to.
  if (!btn.dataset.view) return;
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

// ---------------------------------------------------------------------------
// Health pill
// ---------------------------------------------------------------------------
async function loadHealth() {
  const pill = document.getElementById("health-pill");
  try {
    const health = await api("/health");
    const ok = health.data_generated && health.linkage_run;
    pill.innerHTML = `<span class="w-2 h-2 rounded-full ${ok ? "bg-emerald-400" : "bg-amber-400"}"></span> ${
      ok ? "Linkage up to date" : "Needs generation / linkage"
    }`;
  } catch (e) {
    pill.innerHTML = `<span class="w-2 h-2 rounded-full bg-red-500"></span> API unreachable`;
  }
}

// ---------------------------------------------------------------------------
// Dashboard view
// ---------------------------------------------------------------------------
function kpiCard(label, value, icon, sub) {
  return `
    <div class="wealth-card rounded-lg p-5 flex flex-col justify-between h-28">
      <div class="flex items-center justify-between">
        <span class="text-[11px] uppercase tracking-wider text-on-surface-variant font-semibold">${label}</span>
        <span class="material-symbols-outlined text-primary">${icon}</span>
      </div>
      <div>
        <p class="text-2xl font-bold text-on-surface">${value}</p>
        ${sub ? `<p class="text-[11px] text-on-surface-variant mt-1">${sub}</p>` : ""}
      </div>
    </div>`;
}

async function loadDashboard() {
  const grid = document.getElementById("kpi-grid");
  grid.innerHTML = Array(4)
    .fill('<div class="wealth-card rounded-lg p-5 h-28"><div class="skeleton w-full h-full rounded"></div></div>')
    .join("");

  let d;
  try {
    d = await api("/dashboard");
  } catch (e) {
    showToast(e.message, true);
    return;
  }

  grid.innerHTML = [
    kpiCard("Total Net Wealth", GBP.format(d.total_net_wealth), "account_balance", "Cash + savings + investments − mortgage"),
    kpiCard("Total Assets", GBP.format(d.total_assets), "savings", "Cash + savings + investments"),
    kpiCard("Unique People", INT.format(d.unique_people), "groups", "Ground-truth population generated"),
    kpiCard("Source Records", INT.format(d.source_records), "database", "Noisy records across 5 subsidiaries"),
    kpiCard("Clusters Resolved", INT.format(d.clusters), "bubble_chart", "master_person_id groups"),
    kpiCard("Duplicates Found", INT.format(d.duplicates_found), "auto_fix_high", "Records merged into another cluster"),
    kpiCard("Avg Match Confidence", (d.avg_match_probability * 100).toFixed(2) + "%", "verified", "Mean per-record linkage confidence"),
  ].join("");

  destroyChart("assets");
  charts.assets = new Chart(document.getElementById("chart-assets"), {
    type: "doughnut",
    data: {
      labels: ["Cash", "Savings", "Investments", "Mortgage (liability)"],
      datasets: [
        {
          data: [d.total_cash, d.total_savings, d.total_investments, d.total_mortgage],
          backgroundColor: [CHART_COLORS.primary, CHART_COLORS.accent, CHART_COLORS.primaryDark, CHART_COLORS.danger],
          borderColor: "#1a1c1e",
          borderWidth: 2,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { color: "#a89a8e", font: { size: 11 } } } },
    },
  });

  const subsidiaries = Object.keys(d.subsidiary_record_counts);
  const counts = Object.values(d.subsidiary_record_counts);
  destroyChart("subsidiaries");
  charts.subsidiaries = new Chart(document.getElementById("chart-subsidiaries"), {
    type: "bar",
    data: {
      labels: subsidiaries,
      datasets: [{ data: counts, backgroundColor: CHART_COLORS.primaryDark, borderRadius: 4 }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#a89a8e" }, grid: { color: "#2c2e30" } },
        y: { ticks: { color: "#a89a8e" }, grid: { color: "#2c2e30" } },
      },
    },
  });

  loadShowcase();
}

function showcasePersonCard(r) {
  return `
    <div class="bg-surface-container-high/60 border border-border rounded-lg p-4">
      <div class="flex items-center gap-3 mb-2">
        <div class="w-8 h-8 rounded-full bg-red-500/10 border border-red-500/30 flex items-center justify-center text-xs font-bold text-red-400 shrink-0">
          ${initials(`${r.first_name} ${r.last_name}`)}
        </div>
        <div class="min-w-0">
          <p class="font-semibold text-sm truncate">${r.first_name} ${r.last_name}</p>
          <p class="text-[11px] text-on-surface-variant truncate">${r.subsidiary}</p>
        </div>
      </div>
      <p class="text-xs text-on-surface-variant truncate">${r.email ?? "no email on file"}</p>
      <p class="text-xs text-on-surface-variant truncate">${r.phone ?? "no phone on file"} &middot; ${r.postcode ?? "no postcode"}</p>
    </div>`;
}

async function loadShowcase() {
  const container = document.getElementById("showcase-content");
  container.innerHTML = `<div class="wealth-card rounded-lg p-8"><div class="skeleton w-full h-40 rounded"></div></div>`;

  let s;
  try {
    s = await api("/dashboard/showcase");
  } catch (e) {
    container.innerHTML = `<div class="wealth-card rounded-lg p-8 text-center text-on-surface-variant text-sm">Run linkage first to see a before/after example here.</div>`;
    return;
  }

  container.innerHTML = `
    <div class="grid grid-cols-12 gap-4 items-stretch">
      <div class="col-span-12 lg:col-span-5 border-2 border-dashed border-red-500/30 rounded-lg p-5 bg-red-500/[0.03]">
        <span class="badge bg-red-500/15 text-red-400 mb-3 inline-block">Before Splink</span>
        <p class="text-sm text-on-surface-variant mb-4">${s.record_count} separate subsidiary payroll records look like ${s.record_count} different people</p>
        <div class="space-y-3">
          ${s.linked_records.map(showcasePersonCard).join("")}
        </div>
      </div>

      <div class="col-span-12 lg:col-span-2 flex flex-row lg:flex-col items-center justify-center gap-2 py-4">
        <span class="material-symbols-outlined text-primary text-4xl">arrow_forward</span>
        <span class="text-[11px] uppercase tracking-wider text-on-surface-variant font-semibold">Splink</span>
        ${confidenceBadge(s.match_probability)}
      </div>

      <div class="col-span-12 lg:col-span-5 border-2 border-primary-dark/40 rounded-lg p-5 bg-primary/[0.04] flex flex-col">
        <span class="badge bg-emerald-500/15 text-emerald-400 mb-3 self-start">After Splink</span>
        <div class="flex items-center gap-3 mb-4">
          <div class="w-12 h-12 rounded-full bg-primary-dark/20 border-2 border-primary-dark flex items-center justify-center text-sm font-bold text-primary shrink-0">
            ${initials(s.name)}
          </div>
          <div class="min-w-0">
            <p class="font-bold truncate">${s.name}</p>
            <p class="text-xs font-mono text-on-surface-variant">${s.master_person_id}</p>
          </div>
        </div>
        <p class="text-[11px] text-on-surface-variant">Linked subsidiaries</p>
        <p class="text-sm mb-4">${s.linked_subsidiaries.join(", ")}</p>
        <div class="grid grid-cols-2 gap-3 text-sm mb-4">
          <div><p class="text-[11px] text-on-surface-variant">Salary</p><p class="font-semibold">${GBP.format(s.salary)}</p></div>
          <div><p class="text-[11px] text-on-surface-variant">Net Wealth</p><p class="font-semibold">${GBP.format(s.net_wealth)}</p></div>
        </div>
        <button id="btn-showcase-profile" class="mt-auto text-xs font-bold text-primary hover:underline flex items-center gap-1 self-start">
          View full profile <span class="material-symbols-outlined text-sm">arrow_forward</span>
        </button>
      </div>
    </div>`;

  document.getElementById("btn-showcase-profile").addEventListener("click", () => loadProfile(s.master_person_id));
}

// ---------------------------------------------------------------------------
// Directory / search view
// ---------------------------------------------------------------------------
function confidenceBadge(p) {
  const pct = (p * 100).toFixed(1) + "%";
  if (p >= 0.99) return `<span class="badge bg-emerald-500/15 text-emerald-400">${pct}</span>`;
  if (p >= 0.9) return `<span class="badge bg-amber-500/15 text-amber-400">${pct}</span>`;
  return `<span class="badge bg-red-500/15 text-red-400">${pct}</span>`;
}

async function runSearch(query, { navigate = true } = {}) {
  if (navigate) switchView("directory");

  const empty = document.getElementById("directory-empty");
  const table = document.getElementById("directory-table");
  const caption = document.getElementById("directory-caption");
  const rows = document.getElementById("directory-rows");
  const trimmed = query.trim();
  empty.textContent = trimmed ? "Searching..." : "Loading profiles...";
  empty.classList.remove("hidden");
  table.classList.add("hidden");
  caption.classList.add("hidden");

  let data;
  try {
    data = await api(`/search?q=${encodeURIComponent(trimmed)}`);
  } catch (e) {
    showToast(e.message, true);
    empty.textContent = "Search failed - see toast for details.";
    return;
  }

  if (data.results.length === 0) {
    empty.textContent = trimmed
      ? `No resolved profiles matched "${trimmed}".`
      : "No resolved profiles yet - generate data and run linkage first.";
    empty.classList.remove("hidden");
    table.classList.add("hidden");
    return;
  }

  rows.innerHTML = data.results
    .map(
      (r) => `
        <tr class="hover:bg-surface-container-high transition-colors">
          <td class="px-5 py-3 font-mono text-xs text-primary">${r.master_person_id}</td>
          <td class="px-5 py-3 font-medium">${r.name}</td>
          <td class="px-5 py-3 text-xs text-on-surface-variant">${r.linked_subsidiaries.join(", ")}</td>
          <td class="px-5 py-3 text-right">${GBP.format(r.salary)}</td>
          <td class="px-5 py-3 text-right font-semibold">${GBP.format(r.net_wealth)}</td>
          <td class="px-5 py-3 text-center">${confidenceBadge(r.match_probability)}</td>
          <td class="px-5 py-3 text-center">
            <button class="view-profile text-on-surface-variant hover:text-primary" data-id="${r.master_person_id}">
              <span class="material-symbols-outlined text-base">visibility</span>
            </button>
          </td>
        </tr>`
    )
    .join("");

  caption.textContent = trimmed
    ? `${INT.format(data.total)} match${data.total === 1 ? "" : "es"} for "${trimmed}"`
    : `Showing ${INT.format(data.results.length)} of ${INT.format(data.total)} profiles, A-Z`;
  caption.classList.remove("hidden");

  empty.classList.add("hidden");
  table.classList.remove("hidden");
}

document.getElementById("directory-rows")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".view-profile");
  if (!btn) return;
  loadProfile(btn.dataset.id);
});

document.getElementById("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch(e.target.value);
});

// Clearing the box (without necessarily pressing Enter) reverts to the
// alphabetical listing, but without yanking the user over to the Directory
// tab if they're looking at something else.
document.getElementById("search-input").addEventListener("input", (e) => {
  if (e.target.value.trim() === "") runSearch("", { navigate: false });
});

// ---------------------------------------------------------------------------
// Profile detail page
// ---------------------------------------------------------------------------
const WEALTH_TIER_CLASSES = {
  "Negative Equity": "text-red-400 bg-red-500/10 border-red-500/20",
  "Mass Market": "text-on-surface-variant bg-surface-container-high border-border",
  "Affluent": "text-accent bg-accent/10 border-accent/20",
  "High Net Worth": "text-primary bg-primary/10 border-primary/20",
  "Ultra High Net Worth": "text-primary bg-primary-dark/20 border-primary-dark/40",
};

function initials(name) {
  return name.split(" ").filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
}

function metricCard(label, value, icon, pct, isLiability = false) {
  return `
    <div class="wealth-card rounded-lg p-5">
      <div class="flex justify-between items-center mb-3">
        <span class="text-[11px] uppercase tracking-wider text-on-surface-variant font-semibold">${label}</span>
        <span class="material-symbols-outlined ${isLiability ? "text-red-400" : "text-primary"}">${icon}</span>
      </div>
      <p class="text-xl font-bold ${isLiability ? "text-red-400" : "text-on-surface"}">${GBP.format(value)}</p>
      ${
        pct !== null
          ? `<div class="w-full bg-surface-container-high h-1.5 mt-3 rounded-full overflow-hidden">
               <div class="bg-primary h-full" style="width:${pct}%"></div>
             </div>
             <p class="text-[10px] text-on-surface-variant mt-1">${pct}% of liquid + invested assets</p>`
          : ""
      }
    </div>`;
}

function linkedRecordRow(r) {
  return `
    <div class="p-5 flex items-center justify-between gap-4">
      <div class="flex items-center gap-4 min-w-0">
        <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">
          <span class="material-symbols-outlined text-primary">business</span>
        </div>
        <div class="min-w-0">
          <p class="font-semibold text-sm">${r.subsidiary} <span class="text-on-surface-variant font-normal">&middot; ${r.employee_id}</span></p>
          <p class="text-on-surface-variant text-xs truncate">"${r.first_name} ${r.last_name}" &middot; ${r.email ?? "no email on file"} &middot; ${GBP.format(r.annual_salary)}</p>
        </div>
      </div>
      ${confidenceBadge(r.match_probability)}
    </div>`;
}

function fieldAgreementRow(f) {
  const label = f.field.replace(/_/g, " ");
  if (f.is_consistent) {
    return `
      <div class="flex items-center gap-3 text-sm py-2">
        <span class="material-symbols-outlined text-emerald-400 text-base">check_circle</span>
        <span class="capitalize text-on-surface-variant">${label}</span>
        <span class="ml-auto text-xs text-on-surface-variant">matched exactly</span>
      </div>`;
  }
  return `
    <div class="p-3 rounded-lg bg-amber-500/5 border border-amber-500/20 my-1.5">
      <div class="flex items-center gap-3 text-sm">
        <span class="material-symbols-outlined text-amber-400 text-base">warning</span>
        <span class="capitalize font-medium">${label} varies across linked records</span>
      </div>
      <p class="text-xs text-on-surface-variant mt-1 ml-8">${f.distinct_values.join("  vs.  ")}</p>
    </div>`;
}

function exportProfileJson(profile) {
  const blob = new Blob([JSON.stringify(profile, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${profile.master_person_id}.json`;
  a.click();
  URL.revokeObjectURL(url);
}

async function loadProfile(masterPersonId) {
  switchView("profile");
  const container = document.getElementById("profile-content");
  container.innerHTML = `<div class="wealth-card rounded-lg p-12"><div class="skeleton w-full h-40 rounded"></div></div>`;

  let p;
  try {
    p = await api(`/wealth/${masterPersonId}/detail`);
  } catch (e) {
    showToast(e.message, true);
    container.innerHTML = `<div class="wealth-card rounded-lg p-12 text-center text-on-surface-variant">${e.message}</div>`;
    return;
  }

  const totalLiquid = p.cash + p.savings + p.investments;
  const pct = (v) => (totalLiquid > 0 ? Math.round((v / totalLiquid) * 100) : 0);
  const tierClass = WEALTH_TIER_CLASSES[p.wealth_tier] || WEALTH_TIER_CLASSES["Mass Market"];

  container.innerHTML = `
    <div class="grid grid-cols-12 gap-6">
      <div class="col-span-12 lg:col-span-8 wealth-card rounded-lg p-6 flex items-center gap-6">
        <div class="w-20 h-20 rounded-full bg-primary-dark/20 border-2 border-primary-dark flex items-center justify-center text-2xl font-bold text-primary shrink-0">
          ${initials(p.name)}
        </div>
        <div class="min-w-0">
          <span class="badge border ${tierClass}">${p.wealth_tier}</span>
          <h2 class="text-2xl font-bold mt-2">${p.name}</h2>
          <p class="text-on-surface-variant text-sm flex items-center gap-1 mt-1">
            <span class="material-symbols-outlined text-base">location_on</span>
            ${p.primary_city ? `${p.primary_city} &middot; ${p.primary_postcode ?? ""}` : "Location unknown"}
          </p>
          <p class="text-on-surface-variant text-xs font-mono mt-2">${p.master_person_id}</p>
        </div>
      </div>
      <div class="col-span-12 lg:col-span-4 wealth-card rounded-lg p-6 flex flex-col justify-between">
        <div class="flex justify-between items-center">
          <span class="text-[11px] uppercase tracking-wider text-on-surface-variant font-semibold">Wealth Score</span>
          <span class="material-symbols-outlined text-primary">verified</span>
        </div>
        <div class="flex items-baseline gap-2 mt-2">
          <span class="text-3xl font-bold text-primary">${p.wealth_score.toFixed(1)}</span>
          <span class="text-on-surface-variant text-sm">/ 100 percentile</span>
        </div>
        <div class="w-full bg-surface-container-high h-1.5 mt-3 rounded-full overflow-hidden">
          <div class="bg-primary h-full" style="width:${p.wealth_score}%"></div>
        </div>
      </div>
    </div>

    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      ${metricCard("Salary", p.salary, "payments", null)}
      ${metricCard("Current Account", p.cash, "account_balance_wallet", pct(p.cash))}
      ${metricCard("Savings", p.savings, "savings", pct(p.savings))}
      ${metricCard("Investments", p.investments, "monitoring", pct(p.investments))}
    </div>
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      ${metricCard("Mortgage (liability)", p.mortgage, "domain", null, true)}
      ${metricCard("Net Wealth", p.net_wealth, "account_balance", null)}
    </div>

    <div class="grid grid-cols-12 gap-6">
      <div class="col-span-12 lg:col-span-7 space-y-6">
        <div class="wealth-card rounded-lg overflow-hidden">
          <div class="p-6 border-b border-border flex justify-between items-center">
            <div>
              <h3 class="text-base font-semibold">Data Lineage &amp; Record Linkage</h3>
              <p class="text-on-surface-variant text-xs">Splink entity-resolution results for this profile</p>
            </div>
            ${confidenceBadge(p.match_probability)}
          </div>
          <div class="divide-y divide-border/60">
            ${p.linked_records.map(linkedRecordRow).join("")}
          </div>
        </div>

        <div class="wealth-card rounded-lg p-6">
          <h3 class="text-base font-semibold mb-1">Salary Reported by Subsidiary</h3>
          <p class="text-on-surface-variant text-xs mb-4">Each linked record's reported annual salary</p>
          <div class="h-48"><canvas id="chart-profile-salary"></canvas></div>
        </div>
      </div>

      <div class="col-span-12 lg:col-span-5 space-y-6">
        <div class="wealth-card rounded-lg p-6">
          <h3 class="text-base font-semibold mb-1">Match Explanation</h3>
          <p class="text-on-surface-variant text-xs mb-4">Field-by-field agreement across this profile's ${p.record_count} linked record(s)</p>
          <div>${p.field_agreement.map(fieldAgreementRow).join("")}</div>
        </div>

        <div class="wealth-card rounded-lg p-6">
          <h3 class="text-base font-semibold mb-4">Actions</h3>
          <div class="space-y-1">
            <button id="btn-export-profile" class="w-full flex items-center justify-between p-3 rounded hover:bg-surface-container-high transition-colors text-sm font-medium">
              <span class="flex items-center gap-3"><span class="material-symbols-outlined text-on-surface-variant">download</span> Export Linked Data (JSON)</span>
              <span class="material-symbols-outlined text-xs text-on-surface-variant">chevron_right</span>
            </button>
            <button id="btn-copy-id" class="w-full flex items-center justify-between p-3 rounded hover:bg-surface-container-high transition-colors text-sm font-medium">
              <span class="flex items-center gap-3"><span class="material-symbols-outlined text-on-surface-variant">content_copy</span> Copy Master ID</span>
              <span class="material-symbols-outlined text-xs text-on-surface-variant">chevron_right</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  `;

  document.getElementById("btn-export-profile").addEventListener("click", () => exportProfileJson(p));
  document.getElementById("btn-copy-id").addEventListener("click", () => {
    navigator.clipboard?.writeText(p.master_person_id);
    showToast(`Copied ${p.master_person_id} to clipboard.`);
  });

  destroyChart("profileSalary");
  charts.profileSalary = new Chart(document.getElementById("chart-profile-salary"), {
    type: "bar",
    data: {
      labels: p.linked_records.map((r) => r.subsidiary),
      datasets: [{ data: p.linked_records.map((r) => r.annual_salary), backgroundColor: CHART_COLORS.primary, borderRadius: 4 }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#a89a8e" }, grid: { display: false } },
        y: { ticks: { color: "#a89a8e" }, grid: { color: "#2c2e30" } },
      },
    },
  });
}

document.getElementById("btn-back-directory").addEventListener("click", () => switchView("directory"));

// ---------------------------------------------------------------------------
// Data Quality view
// ---------------------------------------------------------------------------
async function loadQuality() {
  let q;
  try {
    q = await api("/quality");
  } catch (e) {
    showToast(e.message, true);
    return;
  }

  destroyChart("confidence");
  charts.confidence = new Chart(document.getElementById("chart-confidence"), {
    type: "bar",
    data: {
      labels: q.match_probability_histogram.map((b) => b.label),
      datasets: [{ data: q.match_probability_histogram.map((b) => b.count), backgroundColor: CHART_COLORS.primary, borderRadius: 4 }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#a89a8e" }, grid: { display: false } },
        y: { ticks: { color: "#a89a8e" }, grid: { color: "#2c2e30" } },
      },
    },
  });

  destroyChart("clustersize");
  charts.clustersize = new Chart(document.getElementById("chart-clustersize"), {
    type: "bar",
    data: {
      labels: q.cluster_size_distribution.map((b) => b.label + " record(s)"),
      datasets: [{ data: q.cluster_size_distribution.map((b) => b.count), backgroundColor: CHART_COLORS.accent, borderRadius: 4 }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#a89a8e" }, grid: { display: false } },
        y: { ticks: { color: "#a89a8e" }, grid: { color: "#2c2e30" } },
      },
    },
  });

  const rows = document.getElementById("review-queue-rows");
  if (q.review_queue.length === 0) {
    rows.innerHTML = `<tr><td colspan="5" class="px-5 py-8 text-center text-on-surface-variant text-sm">No multi-record clusters found - run linkage first.</td></tr>`;
  } else {
    rows.innerHTML = q.review_queue
      .map(
        (r) => `
        <tr class="hover:bg-surface-container-high transition-colors">
          <td class="px-5 py-3 font-mono text-xs text-primary">${r.master_person_id}</td>
          <td class="px-5 py-3 font-medium">${r.name}</td>
          <td class="px-5 py-3 text-xs text-on-surface-variant">${r.linked_subsidiaries.join(", ")}</td>
          <td class="px-5 py-3 text-center">${r.record_count}</td>
          <td class="px-5 py-3 text-center">${confidenceBadge(r.match_probability)}</td>
        </tr>`
      )
      .join("");
  }
}

// ---------------------------------------------------------------------------
// Pipeline action buttons
// ---------------------------------------------------------------------------
function setBusy(btn, busy, busyLabel) {
  btn.disabled = busy;
  btn.classList.toggle("opacity-50", busy);
  btn.classList.toggle("cursor-not-allowed", busy);
  if (busy) {
    btn.dataset.originalHtml = btn.innerHTML;
    btn.innerHTML = `<span class="material-symbols-outlined text-base animate-spin">progress_activity</span> ${busyLabel}`;
  } else if (btn.dataset.originalHtml) {
    btn.innerHTML = btn.dataset.originalHtml;
  }
}

document.getElementById("btn-generate").addEventListener("click", async () => {
  const btn = document.getElementById("btn-generate");
  setBusy(btn, true, "Generating...");
  try {
    const result = await api("/generate-data", { method: "POST" });
    showToast(`Generated ${INT.format(result.people)} people / ${INT.format(result.records)} records.`);
    await loadHealth();
    if (document.getElementById("view-dashboard").classList.contains("active")) await loadDashboard();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
});

document.getElementById("btn-linkage").addEventListener("click", async () => {
  const btn = document.getElementById("btn-linkage");
  setBusy(btn, true, "Running Splink (~20s)...");
  try {
    const result = await api("/run-linkage", { method: "POST" });
    showToast(`Linkage complete: ${INT.format(result.clusters)} clusters, ${INT.format(result.duplicates_found)} duplicates found.`);
    await loadHealth();
    if (document.getElementById("view-dashboard").classList.contains("active")) await loadDashboard();
    if (document.getElementById("view-quality").classList.contains("active")) await loadQuality();
    if (document.getElementById("view-directory").classList.contains("active")) {
      await runSearch(document.getElementById("search-input").value, { navigate: false });
    }
  } catch (e) {
    showToast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
loadHealth();
loadDashboard();
runSearch("", { navigate: false }); // pre-populate Directory with the A-Z listing
