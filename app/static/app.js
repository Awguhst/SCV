// Single View of Wealth dashboard frontend.
// Plain JS + Chart.js, talking directly to the FastAPI JSON endpoints below.
// No build step / framework - this is a thin presentation layer over the API.

const GBP = new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", maximumFractionDigits: 0 });
const INT = new Intl.NumberFormat("en-GB");

const AUTH_TOKEN_KEY = "svw_token";
const AUTH_ROLE_KEY = "svw_role";
const AUTH_USERNAME_KEY = "svw_username";

const CHART_COLORS = {
  primary: "#ffb779",
  primaryDark: "#cd7f32",
  accent: "#e9c176",
  border: "#2c2e30",
  muted: "#a89a8e",
  danger: "#e57373",
  // Material 3 semantic tokens, added for the tier <-> badge color alignment below.
  // primaryContainer/tertiary are numerically identical to primaryDark/accent
  // (same M3 seed color/theme) - this is a semantic rename, not a color change.
  error: "#ffb4ab",
  secondary: "#c6c6c9",
  primaryContainer: "#cd7f32",
  tertiary: "#e9c176",
};

const SEGMENT_TIER_COLORS = {
  "Negative Equity": CHART_COLORS.error,
  "Mass Market": CHART_COLORS.secondary,
  "Affluent": CHART_COLORS.primary,
  "High Net Worth": CHART_COLORS.primaryContainer,
  "Ultra High Net Worth": CHART_COLORS.tertiary,
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

function authHeaders() {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function clearSession() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_ROLE_KEY);
  localStorage.removeItem(AUTH_USERNAME_KEY);
}

async function api(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
  if (res.status === 401) {
    clearSession();
    showLogin();
    throw new Error("Session expired - please sign in again.");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${path} failed (${res.status})`);
  }
  return res.json();
}

// Authenticated file download - a plain <a href> can't carry the bearer
// token, so this fetches the blob manually and triggers the save itself
// (same client-side download pattern as the existing exportProfileJson,
// just sourced from a server response instead of in-memory JSON).
async function downloadFile(path, fallbackName) {
  let res;
  try {
    res = await fetch(path, { headers: authHeaders() });
  } catch (e) {
    showToast("Download failed - network error.", true);
    return;
  }
  if (res.status === 401) {
    clearSession();
    showLogin();
    return;
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    showToast(body.detail || `Download failed (${res.status})`, true);
    return;
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match ? match[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Auth / session
// ---------------------------------------------------------------------------
function showLogin(message) {
  document.getElementById("login-overlay").classList.remove("hidden");
  document.getElementById("app-shell").classList.add("hidden");
  const err = document.getElementById("login-error");
  if (message) {
    err.textContent = message;
    err.classList.remove("hidden");
  } else {
    err.classList.add("hidden");
  }
}

function hideLogin() {
  document.getElementById("login-overlay").classList.add("hidden");
  document.getElementById("app-shell").classList.remove("hidden");
}

function applyRoleUI(role) {
  const isAdmin = role === "admin";
  document.getElementById("btn-generate").classList.toggle("hidden", !isAdmin);
  document.getElementById("btn-linkage").classList.toggle("hidden", !isAdmin);
}

async function startApp() {
  hideLogin();
  applyRoleUI(localStorage.getItem(AUTH_ROLE_KEY));
  await loadDashboard();
  await runSearch("", { navigate: false }); // pre-populate Directory with the A-Z listing
}

async function restoreSession() {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  if (!token) {
    showLogin();
    return;
  }
  try {
    const res = await fetch("/auth/me", { headers: authHeaders() });
    if (!res.ok) throw new Error("invalid session");
    const me = await res.json();
    localStorage.setItem(AUTH_ROLE_KEY, me.role);
    localStorage.setItem(AUTH_USERNAME_KEY, me.username);
    await startApp();
  } catch (e) {
    clearSession();
    showLogin();
  }
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const submitBtn = document.getElementById("login-submit");
  setBusy(submitBtn, true, "Signing in...");
  try {
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username, password }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "Login failed.");
    }
    const data = await res.json();
    localStorage.setItem(AUTH_TOKEN_KEY, data.access_token);
    localStorage.setItem(AUTH_ROLE_KEY, data.role);
    localStorage.setItem(AUTH_USERNAME_KEY, username);
    document.getElementById("login-password").value = "";
    await startApp();
  } catch (err) {
    showLogin(err.message);
  } finally {
    setBusy(submitBtn, false);
  }
});

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
// The profile page is a sub-page of Directory or Segments (reached only by
// clicking a result row in either list), not a top-level nav destination -
// tracks whichever list view it was opened from so the nav highlight and
// the "Back to ..." button both return there instead of always Directory.
let lastListView = "directory";
const LIST_VIEW_LABELS = { directory: "Back to Directory", segments: "Back to Employee Segments" };

function switchView(view) {
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  document.getElementById(`view-${view}`).classList.add("active");

  const navTarget = view === "profile" ? lastListView : view;
  document.querySelectorAll(".nav-link").forEach((el) => el.classList.remove("active"));
  document.querySelector(`.nav-link[data-view="${navTarget}"]`)?.classList.add("active");

  if (view === "profile") {
    document.getElementById("btn-back-directory-label").textContent = LIST_VIEW_LABELS[lastListView];
  }

  if (view === "dashboard") loadDashboard();
  if (view === "segments") loadSegments();
  if (view === "quality") loadQuality();
  if (view === "settings") loadSettings();
}

document.querySelectorAll(".nav-link").forEach((btn) => {
  // Nav buttons with no data-view (e.g. the cosmetic "Settings" entry) are
  // intentionally inert - nothing to switch to.
  if (!btn.dataset.view) return;
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

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
    kpiCard("Source Records", INT.format(d.source_records), "database", "Payroll + banking product records"),
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

  const productSubsidiaries = Object.keys(d.product_subsidiary_counts);
  const productCounts = Object.values(d.product_subsidiary_counts);
  destroyChart("productSubsidiaries");
  charts.productSubsidiaries = new Chart(document.getElementById("chart-products-subsidiaries"), {
    type: "bar",
    data: {
      labels: productSubsidiaries,
      datasets: [{ data: productCounts, backgroundColor: CHART_COLORS.accent, borderRadius: 4 }],
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

// Normalizes a showcase response's unified `records` list (payroll and
// banking-product rows alike, distinguished by record_type) into one
// "before Splink" evidence list - both kinds are equally noisy, equally
// first-class records now, so the before/after story should show both, not
// just payroll.
function toShowcaseRecords(s) {
  return s.records.map((r) => ({ ...r, type: r.record_type.toLowerCase(), value: r.annual_salary ?? r.balance }));
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
          <p class="text-[11px] text-on-surface-variant truncate">${r.subsidiary} &middot; ${FINANCIAL_TYPE_LABELS[r.type] ?? r.type} ${GBP.format(r.value)}</p>
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

  const showcaseRecords = toShowcaseRecords(s);

  container.innerHTML = `
    <div class="grid grid-cols-12 gap-4 items-stretch">
      <div class="col-span-12 lg:col-span-5 border-2 border-dashed border-red-500/30 rounded-lg p-5 bg-red-500/[0.03]">
        <span class="badge bg-red-500/15 text-red-400 mb-3 inline-block">Before Linking</span>
        <p class="text-sm text-on-surface-variant mb-4">${showcaseRecords.length} separate subsidiary records (payroll and banking products) look like ${showcaseRecords.length} different people</p>
        <div class="space-y-3">
          ${showcaseRecords.map(showcasePersonCard).join("")}
        </div>
      </div>

      <div class="col-span-12 lg:col-span-2 flex flex-row lg:flex-col items-center justify-center gap-2 py-4">
        <span class="material-symbols-outlined text-primary text-4xl">arrow_forward</span>
        <span class="text-[11px] uppercase tracking-wider text-on-surface-variant font-semibold">Probabilistic Linkage</span>
        ${confidenceBadge(s.match_probability)}
      </div>

      <div class="col-span-12 lg:col-span-5 border-2 border-primary-dark/40 rounded-lg p-5 bg-primary/[0.04] flex flex-col">
        <span class="badge bg-emerald-500/15 text-emerald-400 mb-3 self-start">After Linking</span>
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
        ${
          (() => {
            const productRecords = showcaseRecords.filter((r) => r.type !== "payroll");
            return productRecords.length
              ? `<p class="text-[11px] text-on-surface-variant">Banking products</p>
                 <p class="text-sm mb-4">${productRecords.map((r) => `${FINANCIAL_TYPE_LABELS[r.type]} (${r.subsidiary})`).join(", ")}</p>`
              : "";
          })()
        }
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
  lastListView = "directory";
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

document.getElementById("btn-export-directory").addEventListener("click", () => {
  const q = document.getElementById("search-input").value.trim();
  downloadFile(`/export/directory.csv?q=${encodeURIComponent(q)}`, "directory.csv");
});

// ---------------------------------------------------------------------------
// Profile detail page
// ---------------------------------------------------------------------------
const WEALTH_TIER_CLASSES = {
  "Negative Equity": "text-error bg-error/10 border-error/20",
  "Mass Market": "text-secondary bg-secondary/10 border-secondary/20",
  "Affluent": "text-primary bg-primary/10 border-primary/20",
  "High Net Worth": "text-primary-container bg-primary-container/10 border-primary-container/20",
  "Ultra High Net Worth": "text-tertiary bg-tertiary/10 border-tertiary/20",
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

const PRODUCT_TYPE_ICONS = {
  current_account: "account_balance_wallet",
  savings_account: "savings",
  investment: "monitoring",
  mortgage: "domain",
};
const PRODUCT_TYPE_LABELS = {
  current_account: "Current Account",
  savings_account: "Savings Account",
  investment: "Investment",
  mortgage: "Mortgage",
};
const FINANCIAL_TYPE_ICONS = { payroll: "payments", ...PRODUCT_TYPE_ICONS };
const FINANCIAL_TYPE_LABELS = { payroll: "Salary", ...PRODUCT_TYPE_LABELS };

// Orders a profile's unified `records` list (payroll and banking-product
// rows alike, distinguished by record_type) for display - by subsidiary,
// then by record type - so every linked record appears exactly once in one
// itemized view, rather than payroll showing up again as a duplicate
// "Salary" line in a separate section.
function toLinkedRecords(p) {
  return [...p.records].sort(
    (a, b) => a.subsidiary.localeCompare(b.subsidiary) || a.record_type.localeCompare(b.record_type)
  );
}

function recordRow(r) {
  const type = r.record_type.toLowerCase();
  const value = r.annual_salary ?? r.balance;
  const detail = r.employee_id ?? r.account_id;
  return `
    <div class="p-5 flex items-center justify-between gap-4">
      <div class="flex items-center gap-4 min-w-0">
        <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">
          <span class="material-symbols-outlined text-primary">${FINANCIAL_TYPE_ICONS[type] ?? "account_balance"}</span>
        </div>
        <div class="min-w-0">
          <p class="font-semibold text-sm">${FINANCIAL_TYPE_LABELS[type] ?? r.record_type} <span class="text-on-surface-variant font-normal">&middot; ${r.subsidiary} &middot; ${detail}</span></p>
          <p class="text-on-surface-variant text-xs truncate">"${r.first_name} ${r.last_name}" &middot; ${r.email ?? "no email on file"}</p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span class="font-semibold text-sm">${GBP.format(value)}</span>
        ${confidenceBadge(r.match_probability)}
      </div>
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
  const linkedRecords = toLinkedRecords(p);

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
              <p class="text-on-surface-variant text-xs">Every subsidiary record - payroll and banking products alike - resolved into this profile</p>
            </div>
            ${confidenceBadge(p.match_probability)}
          </div>
          <div class="divide-y divide-border/60">
            ${
              linkedRecords.length
                ? linkedRecords.map(recordRow).join("")
                : `<div class="p-5 text-center text-on-surface-variant text-sm">No linked records on file.</div>`
            }
          </div>
        </div>
      </div>

      <div class="col-span-12 lg:col-span-5 space-y-6">
        <div class="wealth-card rounded-lg p-6">
          <h3 class="text-base font-semibold mb-1">Match Explanation</h3>
          <p class="text-on-surface-variant text-xs mb-4">Field-by-field agreement across this profile's ${p.records.length} linked record(s)</p>
          <div>${p.field_agreement.map(fieldAgreementRow).join("")}</div>
        </div>

        <div class="wealth-card rounded-lg p-6">
          <h3 class="text-base font-semibold mb-4">Actions</h3>
          <div class="space-y-1">
            <button id="btn-export-profile" class="w-full flex items-center justify-between p-3 rounded hover:bg-surface-container-high transition-colors text-sm font-medium">
              <span class="flex items-center gap-3"><span class="material-symbols-outlined text-on-surface-variant">download</span> Export Linked Data (JSON)</span>
              <span class="material-symbols-outlined text-xs text-on-surface-variant">chevron_right</span>
            </button>
            <button id="btn-export-profile-pdf" class="w-full flex items-center justify-between p-3 rounded hover:bg-surface-container-high transition-colors text-sm font-medium">
              <span class="flex items-center gap-3"><span class="material-symbols-outlined text-on-surface-variant">picture_as_pdf</span> Export Profile Report (PDF)</span>
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
  document.getElementById("btn-export-profile-pdf").addEventListener("click", () =>
    downloadFile(`/wealth/${p.master_person_id}/export/pdf`, `${p.master_person_id}.pdf`)
  );
  document.getElementById("btn-copy-id").addEventListener("click", () => {
    navigator.clipboard?.writeText(p.master_person_id);
    showToast(`Copied ${p.master_person_id} to clipboard.`);
  });
}

document.getElementById("btn-back-directory").addEventListener("click", () => switchView(lastListView));

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

  const qualityGrid = document.getElementById("quality-kpi-grid");
  qualityGrid.innerHTML = [
    kpiCard("Total Clusters", INT.format(q.total_clusters), "bubble_chart", "Resolved master_person_id groups"),
    kpiCard("Avg Match Confidence", (q.avg_match_probability * 100).toFixed(2) + "%", "verified", "Mean per-record linkage confidence"),
    kpiCard("Multi-Record Clusters", INT.format(q.multi_record_cluster_count), "join", "Clusters with 2+ linked records"),
    kpiCard("High Confidence Records", q.high_confidence_pct.toFixed(2) + "%", "check_circle", "Linked records with match probability >= 0.99"),
  ].join("");

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
    rows.innerHTML = `<tr><td colspan="5" class="px-5 py-8 text-center text-on-surface-variant text-sm">No multi-record clusters currently need review.</td></tr>`;
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

document.getElementById("btn-export-review-queue").addEventListener("click", () => {
  downloadFile("/export/review-queue.csv", "review-queue.csv");
});

// ---------------------------------------------------------------------------
// Employee Segments view
// ---------------------------------------------------------------------------
function segmentCard(s, { isAggregate = false } = {}) {
  const rangeLabel = isAggregate
    ? "All resolved profiles, all tiers"
    : s.min_net_wealth === null
    ? `Below ${GBP.format(s.max_net_wealth)}`
    : s.max_net_wealth === null
    ? `${GBP.format(s.min_net_wealth)}+`
    : `${GBP.format(s.min_net_wealth)} - ${GBP.format(s.max_net_wealth)}`;
  const tierClass = isAggregate
    ? "text-on-surface bg-outline-variant/20 border-outline-variant/40"
    : WEALTH_TIER_CLASSES[s.wealth_tier] || WEALTH_TIER_CLASSES["Mass Market"];
  // The aggregate card is deliberately NOT a `.segment-card` (no data-tier) -
  // there's no /segmentation/{tier}/members-equivalent endpoint for "all
  // tiers combined", so it must stay outside the click-to-drill-down wiring.
  const cardClass = isAggregate ? "wealth-card rounded-xl p-6 text-left w-full" : "segment-card wealth-card rounded-xl p-6 text-left w-full";
  const tierAttr = isAggregate ? "" : ` data-tier="${s.wealth_tier}"`;
  return `
    <button class="${cardClass}"${tierAttr}>
      <div class="flex items-center justify-between mb-2">
        <span class="badge border ${tierClass}">${isAggregate ? "ALL EMPLOYEES" : s.wealth_tier}</span>
        <span class="text-xs text-on-surface-variant">${isAggregate ? "100%" : s.pct_of_population + "%"}</span>
      </div>
      <p class="text-xs text-on-surface-variant/60 mb-3">${rangeLabel}</p>
      <p class="text-2xl font-bold text-on-surface">${INT.format(s.employee_count)}</p>
      <p class="text-[11px] text-on-surface-variant mb-3">employees</p>
      <div class="grid grid-cols-2 gap-2 text-xs border-t border-outline-variant/30 pt-3">
        <div><p class="text-on-surface-variant">Avg net wealth</p><p class="font-semibold">${GBP.format(s.avg_net_wealth)}</p></div>
        <div><p class="text-on-surface-variant">Total net wealth</p><p class="font-semibold">${GBP.format(s.total_net_wealth)}</p></div>
        <div><p class="text-on-surface-variant">Avg salary</p><p class="font-semibold">${GBP.format(s.avg_salary)}</p></div>
        <div><p class="text-on-surface-variant">Avg savings</p><p class="font-semibold">${GBP.format(s.avg_savings)}</p></div>
      </div>
    </button>`;
}

// Whole-book aggregate for the 6th Employee Segments card slot - computed
// entirely from the existing /segmentation response (no backend change).
// avg_salary/avg_savings must be employee-count-weighted across the 5 tiers,
// since an unweighted mean of the tiers' averages would be wrong given how
// unevenly populated they are (e.g. Mass Market dwarfs Ultra High Net Worth).
function computeAggregateSegment(d) {
  const totalProfiles = d.total_profiles || 0;
  const totalNetWealth = d.segments.reduce((sum, s) => sum + s.total_net_wealth, 0);
  const weightedSalary = d.segments.reduce((sum, s) => sum + s.avg_salary * s.employee_count, 0);
  const weightedSavings = d.segments.reduce((sum, s) => sum + s.avg_savings * s.employee_count, 0);
  return {
    wealth_tier: "All Employees",
    employee_count: totalProfiles,
    total_net_wealth: totalNetWealth,
    avg_net_wealth: totalProfiles ? totalNetWealth / totalProfiles : 0,
    avg_salary: totalProfiles ? weightedSalary / totalProfiles : 0,
    avg_savings: totalProfiles ? weightedSavings / totalProfiles : 0,
  };
}

async function loadSegments() {
  const grid = document.getElementById("segment-cards");
  grid.innerHTML = Array(6)
    .fill('<div class="wealth-card rounded-xl p-6 h-48"><div class="skeleton w-full h-full rounded"></div></div>')
    .join("");
  document.getElementById("segment-members-panel").classList.add("hidden");

  let d;
  try {
    d = await api("/segmentation");
  } catch (e) {
    showToast(e.message, true);
    grid.innerHTML = `<div class="wealth-card rounded-xl p-8 text-center text-on-surface-variant text-sm col-span-full">${e.message}</div>`;
    return;
  }

  grid.innerHTML =
    d.segments.map((s) => segmentCard(s)).join("") + segmentCard(computeAggregateSegment(d), { isAggregate: true });

  destroyChart("segmentPopulation");
  charts.segmentPopulation = new Chart(document.getElementById("chart-segment-population"), {
    type: "doughnut",
    data: {
      labels: d.segments.map((s) => s.wealth_tier),
      datasets: [
        {
          data: d.segments.map((s) => s.employee_count),
          backgroundColor: d.segments.map((s) => SEGMENT_TIER_COLORS[s.wealth_tier]),
          borderColor: "#1a1c1e",
          borderWidth: 2,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
    },
  });

  document.getElementById("segment-population-legend").innerHTML = d.segments
    .map(
      (s) => `
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full shrink-0" style="background-color:${SEGMENT_TIER_COLORS[s.wealth_tier]}"></span>
          <span class="text-xs text-on-surface-variant truncate">${s.wealth_tier}</span>
        </div>`
    )
    .join("");

  destroyChart("segmentWealth");
  charts.segmentWealth = new Chart(document.getElementById("chart-segment-wealth"), {
    type: "bar",
    data: {
      labels: d.segments.map((s) => s.wealth_tier),
      datasets: [
        {
          data: d.segments.map((s) => s.total_net_wealth),
          backgroundColor: d.segments.map((s) => SEGMENT_TIER_COLORS[s.wealth_tier]),
          borderRadius: 4,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: "#d8c2b2" }, grid: { display: false } },
        y: { ticks: { color: "#d8c2b2" }, grid: { color: "#2c2e30" } },
      },
    },
  });
}

document.getElementById("segment-cards").addEventListener("click", (e) => {
  const card = e.target.closest(".segment-card");
  if (!card) return;
  loadSegmentMembers(card.dataset.tier);
});

async function loadSegmentMembers(tier) {
  lastListView = "segments";
  const panel = document.getElementById("segment-members-panel");
  const title = document.getElementById("segment-members-title");
  const caption = document.getElementById("segment-members-caption");
  const rows = document.getElementById("segment-members-rows");

  panel.classList.remove("hidden");
  panel.dataset.tier = tier;
  title.textContent = `${tier} - Members`;
  caption.textContent = "Loading members...";
  rows.innerHTML = "";
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });

  let data;
  try {
    data = await api(`/segmentation/${encodeURIComponent(tier)}/members`);
  } catch (e) {
    showToast(e.message, true);
    caption.textContent = "Failed to load members - see toast for details.";
    return;
  }

  if (data.results.length === 0) {
    caption.textContent = `No employees currently in "${tier}".`;
    rows.innerHTML = "";
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
            <button class="view-segment-profile text-on-surface-variant hover:text-primary" data-id="${r.master_person_id}">
              <span class="material-symbols-outlined text-base">visibility</span>
            </button>
          </td>
        </tr>`
    )
    .join("");

  caption.textContent = `Showing ${INT.format(data.results.length)} of ${INT.format(data.total)} employees in "${tier}"`;
}

document.getElementById("segment-members-rows").addEventListener("click", (e) => {
  const btn = e.target.closest(".view-segment-profile");
  if (!btn) return;
  loadProfile(btn.dataset.id);
});

document.getElementById("btn-close-segment-members").addEventListener("click", () => {
  document.getElementById("segment-members-panel").classList.add("hidden");
});

document.getElementById("btn-export-segment-members").addEventListener("click", () => {
  const tier = document.getElementById("segment-members-panel").dataset.tier;
  if (!tier) return;
  downloadFile(`/export/segment-members.csv?tier=${encodeURIComponent(tier)}`, `segment-${tier.toLowerCase().replace(/ /g, "-")}.csv`);
});

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

function activeView() {
  return document.querySelector(".view.active")?.id.replace("view-", "");
}

async function generateData(btn) {
  setBusy(btn, true, "Generating...");
  try {
    const result = await api("/generate-data", { method: "POST" });
    showToast(`Generated ${INT.format(result.people)} people / ${INT.format(result.records)} records.`);
    const view = activeView();
    if (view === "dashboard") await loadDashboard();
    if (view === "segments") await loadSegments();
  } catch (e) {
    showToast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

async function runLinkage(btn) {
  setBusy(btn, true, "Running linkage (~20s)...");
  try {
    const result = await api("/run-linkage", { method: "POST" });
    showToast(`Linkage complete: ${INT.format(result.clusters)} clusters, ${INT.format(result.duplicates_found)} duplicates found.`);
    const view = activeView();
    if (view === "dashboard") await loadDashboard();
    if (view === "segments") await loadSegments();
    if (view === "quality") await loadQuality();
    if (view === "directory") {
      await runSearch(document.getElementById("search-input").value, { navigate: false });
    }
  } catch (e) {
    showToast(e.message, true);
  } finally {
    setBusy(btn, false);
  }
}

document.getElementById("btn-generate").addEventListener("click", (e) => generateData(e.currentTarget));
document.getElementById("btn-linkage").addEventListener("click", (e) => runLinkage(e.currentTarget));

// ---------------------------------------------------------------------------
// Settings view
// ---------------------------------------------------------------------------
const ROLE_BADGE_CLASSES = {
  admin: "text-primary bg-primary/10 border-primary/20",
  analyst: "text-secondary bg-secondary/10 border-secondary/20",
};

function loadSettings() {
  const username = localStorage.getItem(AUTH_USERNAME_KEY);
  const role = localStorage.getItem(AUTH_ROLE_KEY);
  document.getElementById("settings-username").textContent = username;
  document.getElementById("settings-role").innerHTML =
    `<span class="badge border ${ROLE_BADGE_CLASSES[role] || ROLE_BADGE_CLASSES.analyst}">${role}</span>`;
}

document.getElementById("btn-logout-settings").addEventListener("click", () => {
  clearSession();
  showLogin();
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
restoreSession();
