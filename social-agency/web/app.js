/* Agency Console — single-page dashboard. Every number shown comes from the API; nothing is mocked. */
"use strict";

const NAV = [
  ["overview", "Overview"], ["audit", "Account Audit"], ["library", "Content Library"], ["studio", "AI Content Studio"],
  ["calendar", "Content Calendar"], ["queue", "Publishing Queue"], ["analytics", "Analytics"],
  ["audience", "Audience Insights"], ["experiments", "Growth Experiments"], ["community", "Community"],
  ["reports", "Reports"], ["brand", "Brand Settings"], ["integrations", "Integrations"],
  ["automation", "Automation Controls"], ["system", "System Health & Costs"],
];

const state = { session: null, accounts: [], accountId: null, charts: [] };

// ------------------------------------------------------------------ utils
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (v) => (v === null || v === undefined ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }));
const fmtPct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
const fmtDate = (v) => (v ? new Date(v.endsWith("Z") || v.includes("+") ? v : v + "Z").toLocaleString() : "—");
const fmtDay = (v) => (v ? new Date(v.endsWith("Z") || v.includes("+") ? v : v + "Z").toLocaleDateString() : "—");
const ago = (v) => { if (!v) return "never"; const s = (Date.now() - new Date(v + (v.includes("+") || v.endsWith("Z") ? "" : "Z"))) / 1000;
  if (s < 90) return "just now"; if (s < 5400) return Math.round(s / 60) + " min ago"; if (s < 129600) return Math.round(s / 3600) + " h ago"; return Math.round(s / 86400) + " days ago"; };
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: { "X-Requested-With": "agency" }, credentials: "same-origin" };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith("/api/session")) { await boot(); throw new Error("Signed out"); }
  const text = await res.text();
  const data = text ? (() => { try { return JSON.parse(text); } catch { return text; } })() : null;
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function toast(msg, ms = 3500) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms); }
async function act(fn, ok) { try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast("⚠ " + e.message, 6000); throw e; } }
const statusBadge = (s) => {
  const map = { published: "good", approved: "good", ready: "good", connected: "good", succeeded: "good", sent: "good",
    scheduled: "info", publishing: "info", analyzing: "info", editing: "info", new: "info", queued: "info", running: "info", drafted: "info",
    needs_review: "warn", draft: "warn", reconnect_required: "warn", not_connected: "warn",
    failed: "danger", dead: "danger", error: "danger", cancelled: "" };
  return `<span class="badge ${map[s] ?? ""}">${esc(String(s).replace(/_/g, " "))}</span>`;
};
const empty = (title, text) => `<div class="empty"><b>${esc(title)}</b>${text}</div>`;
const info = (text) => `<span class="info" title="${esc(text)}">i</span>`;
function destroyCharts() { state.charts.forEach((c) => c.destroy()); state.charts = []; }
const acct = () => state.accounts.find((a) => a.id === state.accountId);
const needAccount = () => (acct() ? null : empty("No account yet", `Create your first account on the <a href="#/integrations">Integrations</a> page.`));

function lineChart(canvas, points, label, color = "--series-1") {
  if (!window.Chart) { canvas.parentElement.innerHTML = `<p class="muted small">Chart library unavailable offline. ${points.length} data points.</p>`; return; }
  const c = new Chart(canvas, {
    type: "line",
    data: { labels: points.map((p) => p.day), datasets: [{ label, data: points.map((p) => p.value), borderColor: css(color),
      backgroundColor: css(color), borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, tension: 0.25 }] },
    options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => `${label}: ${fmtN(ctx.parsed.y)}` } } },
      scales: { x: { grid: { display: false }, ticks: { color: css("--text-muted"), maxTicksLimit: 8 } },
        y: { beginAtZero: true, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--text-muted") } } } },
  });
  state.charts.push(c);
}

function bars(data, total) {
  const entries = Object.entries(data).slice(0, 12);
  const max = Math.max(...entries.map(([, v]) => v), 1);
  return entries.map(([k, v]) => `<div class="bar-row"><span>${esc(k)}</span><div class="bar-track"><div class="bar-fill" style="width:${(100 * v / max).toFixed(1)}%"></div></div>
    <span class="num small">${fmtN(v)}${total ? ` · ${(100 * v / total).toFixed(0)}%` : ""}</span></div>`).join("");
}

function openDrawer(html) { const d = $("#drawer"); d.innerHTML = `<button class="btn ghost close" onclick="closeDrawer()">✕ Close</button>` + html; d.classList.remove("hidden"); d.scrollTop = 0; }
window.closeDrawer = () => $("#drawer").classList.add("hidden");

// ------------------------------------------------------------------- boot
async function boot() {
  state.session = await api("/api/session");
  if (!state.session.user) return renderAuth();
  $("#auth").classList.add("hidden");
  $("#shell").classList.remove("hidden");
  $("#nav").innerHTML = NAV.map(([k, v]) => `<a href="#/${k}" data-k="${k}">${esc(v)}</a>`).join("");
  await loadAccounts();
  renderWarnings();
  renderPause(state.session.publishing_paused);
  pollNotifications();
  route();
}

function renderAuth() {
  $("#shell").classList.add("hidden");
  const setup = state.session.needs_setup;
  const el = $("#auth");
  el.classList.remove("hidden");
  el.innerHTML = `<div class="card"><h1>${setup ? "Set up your agency" : "Sign in"}</h1>
    <p class="sub">${setup ? "Create the owner login. You can invite editors and viewers later." : "Agency Console"}</p>
    <form id="authForm"><label class="field"><span>Email</span><input name="email" type="email" required autocomplete="username"></label>
    <label class="field"><span>Password${setup ? " (10+ characters)" : ""}</span><input name="password" type="password" required minlength="${setup ? 10 : 1}" autocomplete="${setup ? "new-password" : "current-password"}"></label>
    <button class="btn primary" style="width:100%">${setup ? "Create owner account" : "Sign in"}</button></form></div>`;
  $("#authForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    await act(() => api(setup ? "/api/setup" : "/api/login", { method: "POST", body: { email: f.get("email"), password: f.get("password") } }));
    boot();
  };
}

async function loadAccounts() {
  state.accounts = await api("/api/accounts");
  const saved = Number(localStorageGet("accountId"));
  state.accountId = state.accounts.find((a) => a.id === saved)?.id ?? state.accounts[0]?.id ?? null;
  $("#accountSelect").innerHTML = state.accounts.length
    ? state.accounts.map((a) => `<option value="${a.id}" ${a.id === state.accountId ? "selected" : ""}>${esc(a.name)}${a.ig_username ? " · @" + esc(a.ig_username) : ""}</option>`).join("")
    : `<option>No accounts</option>`;
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

function renderWarnings() {
  const w = state.session.warnings || [];
  $("#warnings").innerHTML = w.map((x) => `<div class="alert warn">${esc(x)}</div>`).join("");
}

function renderPause(paused) {
  $("#pauseBadge").classList.toggle("hidden", !paused);
  const b = $("#pauseBtn");
  b.textContent = paused ? "Resume publishing" : "Pause publishing";
  b.className = paused ? "btn primary" : "btn danger-outline";
  b.dataset.paused = paused ? "1" : "";
}

async function pollNotifications() {
  try {
    const n = await api("/api/notifications");
    const unread = n.filter((x) => !x.read).length;
    const dot = $("#notifCount");
    dot.textContent = unread; dot.classList.toggle("hidden", !unread);
  } catch { /* ignore */ }
  clearTimeout(state._notif); state._notif = setTimeout(pollNotifications, 60000);
}

$("#accountSelect").onchange = (e) => { state.accountId = Number(e.target.value); localStorageSet("accountId", state.accountId); route(); };
$("#pauseBtn").onclick = async () => {
  const pausing = !$("#pauseBtn").dataset.paused;
  if (pausing && !confirm("Stop all new publishing immediately across every account?")) return;
  const r = await act(() => api("/api/automation/pause", { method: "POST", body: { paused: pausing } }), pausing ? "Publishing paused" : "Publishing resumed");
  renderPause(r.publishing_paused);
};
$("#logoutBtn").onclick = async () => { await api("/api/logout", { method: "POST" }); boot(); };
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next; localStorageSet("theme", next); route();
};
$("#menuBtn").onclick = () => $(".sidebar").classList.toggle("open");
$("#notifBtn").onclick = async () => {
  const n = await api("/api/notifications");
  openDrawer(`<h1>Notifications</h1><ul class="list">${n.map((x) => `<li><span class="badge ${x.level === "error" ? "danger" : x.level === "warning" ? "warn" : ""}">${esc(x.level)}</span>
    <b>${esc(x.title)}</b> <span class="muted small">${fmtDate(x.ts)}</span><div class="small">${esc(x.message)}</div>${x.link ? `<a class="small" href="${esc(x.link)}" onclick="closeDrawer()">Open</a>` : ""}</li>`).join("") || "<li class='muted'>Nothing yet.</li>"}</ul>`);
  await api("/api/notifications/read", { method: "POST" }); pollNotifications();
};
{ const t = localStorageGet("theme"); if (t) document.documentElement.dataset.theme = t; }

window.addEventListener("hashchange", route);
function params() { return new URLSearchParams(location.hash.split("?")[1] || ""); }

async function route() {
  if (!state.session?.user) return;
  destroyCharts();
  closeDrawer();
  const key = (location.hash.slice(2).split("?")[0]) || "overview";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.k === key));
  $(".sidebar").classList.remove("open");
  const view = $("#view");
  view.innerHTML = `<p class="muted">Loading…</p>`;
  try { await (VIEWS[key] || VIEWS.overview)(view); } catch (e) { view.innerHTML = `<div class="alert bad">${esc(e.message)}</div>`; }
}

// ================================================================== views
const VIEWS = {};

// ---------------------------------------------------------------- overview
VIEWS.overview = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const days = Number(params().get("days") || 30);
  const end = new Date(Date.now() + 86400000).toISOString().slice(0, 10);
  const start = new Date(Date.now() - (days - 1) * 86400000).toISOString().slice(0, 10);
  const [ov, reach, followers, growth] = await Promise.all([
    api(`/api/analytics/overview?account_id=${state.accountId}&start=${start}&end=${end}`),
    api(`/api/analytics/series?account_id=${state.accountId}&metric=reach_total&start=${start}&end=${end}`),
    api(`/api/analytics/series?account_id=${state.accountId}&metric=followers_count&start=${start}&end=${end}`),
    api(`/api/analytics/growth?account_id=${state.accountId}&days=${Math.min(days, 30)}`),
  ]);
  const c = ov.current, p = ov.previous, a = acct();
  const tile = (key, label, fmt = fmtN, def = "") => {
    const cur = c[key] || {}; const prev = p[key] || {};
    let delta = "";
    if (cur.value != null && prev.value != null && prev.value !== 0) { const d = (cur.value - prev.value) / Math.abs(prev.value); delta = `${d >= 0 ? "▲" : "▼"} ${(Math.abs(d) * 100).toFixed(1)}% vs previous ${days}d`; }
    return `<div class="card kpi"><div class="label">${esc(label)} ${def ? info(def) : ""}</div>
      ${cur.value == null ? `<div class="na">${esc(cur.unavailable || "Data unavailable from connected source")}</div>` : `<div class="value">${fmt(cur.value)}</div><div class="delta">${delta}</div>`}</div>`;
  };
  const D = c.definitions;
  v.innerHTML = `<div class="row"><div><h1>Overview</h1><p class="sub">${esc(a.name)} · data synced ${ago(c.data_freshness.insights)}</p></div><div class="grow"></div>
      <div class="tabs">${[7, 30, 90].map((d) => `<button class="${d === days ? "on" : ""}" onclick="location.hash='#/overview?days=${d}'">Last ${d} days</button>`).join("")}</div></div>
    ${a.connection_status !== "connected" ? `<div class="alert warn" style="margin:0 0 14px">This account is not connected, so there is no Instagram data yet. <a href="#/integrations">Connect it</a>.</div>` : ""}
    <div class="kpis">
      ${tile("followers", "Followers", fmtN, D.followers)}${tile("net_follower_growth", "Net follower growth", fmtN, D.net_follower_growth)}
      ${tile("followers_gained", "Followers gained", fmtN, D["follows / unfollows"])}${tile("followers_lost", "Followers lost", fmtN, D["follows / unfollows"])}
      ${tile("account_reach", "Account reach (sum of daily)", fmtN, D.account_reach)}${tile("views", "Views", fmtN, D.views)}
      ${tile("total_interactions", "Interactions", fmtN, D.interactions)}${tile("engagement_rate_by_reach", "Engagement rate (by reach)", fmtPct, D.engagement_rate_by_reach)}
      ${tile("shares", "Shares")}${tile("saves", "Saves")}${tile("comments", "Comments")}${tile("likes", "Likes")}
      ${tile("profile_link_taps", "Profile link taps")}${tile("post_reach_sum", "Post reach (summed)", fmtN, D.post_reach_sum)}${tile("impressions", "Impressions")}
      <div class="card kpi"><div class="label">Content published</div><div class="value">${fmtN(c.content_published.value)}</div><div class="delta">${Object.entries(c.content_published.by_format || {}).map(([k, n]) => `${n} ${k}`).join(" · ")}</div></div>
    </div>
    <div class="grid g2">
      <div class="card"><h2>Daily account reach</h2>${reach.length ? `<div class="chart-box"><canvas id="cReach"></canvas></div>` : empty("No reach data yet", "Appears after the first sync.")}</div>
      <div class="card"><h2>Followers (daily snapshots)</h2>${followers.length > 1 ? `<div class="chart-box"><canvas id="cFol"></canvas></div>` : empty("Building history", "One snapshot is stored per day from the day you connect.")}</div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h2>Best-performing content</h2>${c.best_content.length ? `<ul class="list">${c.best_content.map((r) => `<li><b>${fmtN(r.metrics.reach)}</b> reach · ${esc(r.format)} · ${fmtDay(r.posted_at)}
        ${r.relative_reach ? `<span class="badge ${r.relative_reach >= 1 ? "good" : ""}">${r.relative_reach}× baseline</span>` : ""}<div class="small muted">${esc(r.caption.slice(0, 110))}</div>
        ${r.permalink ? `<a class="small" href="${esc(r.permalink)}" target="_blank" rel="noopener">View on Instagram</a>` : ""}</li>`).join("")}</ul>` : empty("No posts in this period", "")}</div>
      <div class="card"><h2>Growth intelligence</h2>${growthBlock(growth.insights)}</div>
    </div>
    <div class="card" style="margin-top:14px"><h2>Targets this month</h2>${targetsBlock(growth.targets)}</div>
    <div class="card" style="margin-top:14px"><h2>Business outcomes</h2>${c.conversions.note ? `<p class="muted">${esc(c.conversions.note)}</p>` :
      `<table><tr><th>Metric</th><th class="num">Value</th><th>Measurement method</th></tr>${Object.entries(c.conversions).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${fmtN(x.value)}</td><td class="small">${esc(x.method)}</td></tr>`).join("")}</table>`}
      <p class="small muted">Sales are attributed to Instagram only through the stated measurement method; unattributed gaps are not filled in.</p></div>`;
  if (reach.length) lineChart($("#cReach"), reach, "Reach");
  if (followers.length > 1) lineChart($("#cFol"), followers, "Followers", "--series-3");
};

function growthBlock(g) {
  if (!g || g.status === "insufficient_data") return `<p class="muted">${esc(g?.actions?.[0]?.why || "Not enough data yet.")}</p>`;
  return `<h3>Observed facts <span class="badge">from synced data</span></h3><ul class="list">${g.facts.map((f) => `<li>${esc(f.fact)}</li>`).join("") || "<li class='muted'>None</li>"}</ul>
    <h3>Hypotheses <span class="badge warn">not proven</span></h3><ul class="list">${g.hypotheses.map((h) => `<li>${esc(h.hypothesis)} <span class="badge">${esc(h.confidence)} confidence</span></li>`).join("") || "<li class='muted'>None yet</li>"}</ul>
    <h3>Recommended next actions</h3><ul class="list">${g.actions.map((x) => `<li><b>${esc(x.action)}</b><div class="small muted">${esc(x.why)}</div></li>`).join("")}</ul>`;
}
function targetsBlock(t) {
  if (!t || t.note) return `<p class="muted">${esc(t?.note || "")} <a href="#/automation">Set targets</a></p>`;
  return `<table><tr><th>Metric</th><th class="num">Target</th><th class="num">Actual</th><th class="num">Projected</th><th>Status</th></tr>
    ${Object.entries(t).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${fmtN(x.target)}</td><td class="num">${fmtN(x.actual)}</td><td class="num">${fmtN(x.projected)}</td>
    <td>${x.on_track == null ? "<span class='badge'>no data</span>" : x.on_track ? "<span class='badge good'>✓ on track</span>" : "<span class='badge warn'>! behind</span>"}</td></tr>`).join("")}</table>`;
}

// ------------------------------------------------------------------- audit
VIEWS.audit = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const latest = await api(`/api/audit/${state.accountId}`);
  const render = (r) => {
    if (!r) return empty("No audit yet", `Connect and sync the account, then run the audit.`);
    const d = r.data, h = d.health, n = d.narrative;
    return `<div class="grid g3"><div class="card"><h2>Account health</h2><div class="score">${h.score ?? "—"}<span class="muted small">/100</span></div><p class="small muted">${esc(h.coverage)}</p></div>
      <div class="card" style="grid-column: span 2"><h2>Score methodology</h2><table><tr><th>Component</th><th class="num">Weight</th><th class="num">Score</th><th>How it is calculated</th></tr>
      ${h.components.map((c) => `<tr><td>${esc(c.component.replace(/_/g, " "))}</td><td class="num">${c.weight}</td><td class="num">${c.score ?? "<span class='muted'>no data</span>"}</td><td class="small">${esc(c.method)}<div class="muted">${esc(JSON.stringify(c.evidence))}</div></td></tr>`).join("")}</table></div></div>
      ${n ? `<div class="card" style="margin-top:14px"><h2>Executive summary <span class="badge">${esc(d.narrative_source)}</span></h2><p>${esc(n.executive_summary)}</p></div>` : `<div class="alert warn" style="margin:14px 0 0">AI narrative ${esc(d.narrative_source)}. Rule-based findings are shown below.</div>`}
      <div class="grid g2" style="margin-top:14px">
        <div class="card"><h2>Growth opportunities</h2>${recList((n?.opportunities?.length ? n.opportunities : d.findings.opportunities))}</div>
        <div class="card"><h2>Weaknesses</h2>${recList((n?.weaknesses?.length ? n.weaknesses : d.findings.weaknesses))}</div>
      </div>
      <div class="grid g2" style="margin-top:14px">
        <div class="card"><h2>Best categories & gaps</h2>${n ? `<p><b>Best:</b> ${esc((n.best_categories || []).join(", ") || "—")}</p><p><b>Gaps:</b> ${esc((n.content_gaps || []).join(", ") || "—")}</p>` : ""}
          <h3>Performance by format (120 days)</h3>${fmtTable(d.performance.by_format)}<h3>By pillar</h3>${fmtTable(d.performance.by_pillar)}</div>
        <div class="card"><h2>30-day roadmap</h2>${recList(n?.roadmap_30_days?.length ? n.roadmap_30_days : d.findings.roadmap)}
          <h3>Competitor intelligence</h3><p class="small muted">${esc(d.competitors.note)}</p><p class="small">Listed: ${esc((d.competitors.listed || []).join(", ") || "none")}</p></div>
      </div><p class="small muted">Generated ${fmtDate(d.generated_at)}. ${esc(d.labels.findings)}; narrative: ${esc(d.labels.narrative)}.</p>`;
  };
  v.innerHTML = `<div class="row"><div><h1>Account Audit</h1><p class="sub">Profile, historical performance, content gaps and a prioritised roadmap.</p></div><div class="grow"></div>
    <button class="btn primary" id="runAudit">Run audit now</button></div><div id="auditBody">${render(latest)}</div>`;
  $("#runAudit").onclick = async () => { $("#runAudit").disabled = true; const r = await act(() => api(`/api/audit/${state.accountId}`, { method: "POST" }), "Audit complete").finally(() => ($("#runAudit").disabled = false)); $("#auditBody").innerHTML = render(r); };
};
const recList = (items) => (items && items.length ? `<ul class="list">${items.map((x) => typeof x === "string" ? `<li>${esc(x)}</li>` :
  `<li><b>${esc(x.item || x.focus || x.title || x.action || ("Week " + (x.week || "")))}</b> ${x.priority ? `<span class="badge ${x.priority === "high" ? "danger" : "warn"}">${esc(x.priority)}</span>` : ""}
  ${x.week && x.focus ? "" : ""}<div class="small">${["why", "action", "success", "why_it_matters", "success_criteria", "measure"].filter((k) => x[k]).map((k) => `<div><span class="muted">${esc(k.replace(/_/g, " "))}:</span> ${esc(x[k])}</div>`).join("")}</div></li>`).join("")}</ul>` : `<p class="muted">None.</p>`);
const fmtTable = (obj) => (Object.keys(obj || {}).length ? `<table><tr><th></th><th class="num">Posts</th><th class="num">Median reach</th><th class="num">Median ER</th></tr>${Object.entries(obj).map(([k, x]) =>
  `<tr><td>${esc(k)}</td><td class="num">${x.posts}</td><td class="num">${fmtN(x.median_reach)}</td><td class="num">${fmtPct(x.median_er_reach)}</td></tr>`).join("")}</table>` : `<p class="muted small">No data.</p>`);

// ----------------------------------------------------------------- library
VIEWS.library = async (v) => {
  const p = params(); const status = p.get("status") || ""; const review = p.get("review") === "1";
  const q = review ? "unassigned=true" : `account_id=${state.accountId || ""}${status ? "&status=" + status : ""}`;
  const assets = await api(`/api/assets?${q}`);
  const a = acct();
  v.innerHTML = `<h1>Content Library</h1><p class="sub">Drop files here or into <code>${esc(a ? a.folder_name + "/Inbox" : "your account Inbox folder")}</code>. Originals are never changed or deleted.</p>
    <div class="drop" id="drop">Drag photos and videos here, or <label class="btn">Choose files<input type="file" id="fileIn" multiple accept="image/*,video/*" hidden></label>
      <div class="row" style="justify-content:center;margin-top:10px"><select id="upAcct"><option value="">Shared Assets (review queue)</option>${state.accounts.map((x) => `<option value="${x.id}" ${x.id === state.accountId ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
      <input id="upCampaign" placeholder="Campaign / group (optional)"></div><div id="upStatus" class="small muted"></div></div>
    <div class="tabs">${["", "needs_review", "analyzing", "editing", "ready", "scheduled", "published", "failed"].map((s) => `<button class="${!review && s === status ? "on" : ""}" onclick="location.hash='#/library?status=${s}'">${s ? esc(s.replace("_", " ")) : "All"}</button>`).join("")}
      <button class="${review ? "on" : ""}" onclick="location.hash='#/library?review=1'">Unassigned review queue</button></div>
    ${assets.length ? `<div class="thumbs">${assets.map((x) => `<div class="thumb" onclick="assetDetail(${x.id})"><div class="img" style="${x.preview_url ? `background-image:url('${x.preview_url}')` : ""}">${x.preview_url ? "" : esc(x.media_kind)}</div>
      <div class="meta"><div class="name" title="${esc(x.filename)}">${esc(x.filename)}</div><div class="row small">${statusBadge(x.status)} ${x.media_kind === "video" ? "🎬" : ""} ${x.rights_confirmed ? "" : "<span class='badge warn'>rights?</span>"}</div></div></div>`).join("")}</div>`
      : empty(review ? "Review queue is empty" : "No assets yet", "Upload media above or sync your folder. The worker checks for new files every few seconds.")}`;
  const upload = async (files) => {
    for (const f of files) {
      const fd = new FormData(); fd.append("file", f); if ($("#upAcct").value) fd.append("account_id", $("#upAcct").value); fd.append("campaign", $("#upCampaign").value);
      $("#upStatus").textContent = `Uploading ${f.name}…`;
      await act(() => api("/api/upload", { method: "POST", body: fd }));
    }
    $("#upStatus").textContent = `${files.length} file(s) uploaded. Processing starts on the next worker scan.`;
  };
  $("#fileIn").onchange = (e) => upload([...e.target.files]);
  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload([...e.dataTransfer.files]); };
};

window.assetDetail = async (id) => {
  const a = await api(`/api/assets/${id}`);
  const ai = a.analysis?.ai;
  openDrawer(`<h1>${esc(a.filename)}</h1><p>${statusBadge(a.status)} <span class="muted small">${esc(a.status_reason)}</span></p>
    <div class="grid g2">${a.derivatives.filter((d) => ["feed", "reel", "cover", "portrait"].includes(d.kind)).map((d) => d.url && d.kind === "reel" ? `<video src="${d.url}" controls style="width:100%;border-radius:8px"></video>` : `<img src="${d.url}" style="width:100%;border-radius:8px" alt="${esc(d.kind)}">`).join("") || "<p class='muted'>No exports yet.</p>"}</div>
    <p class="small"><a href="${a.original_url}" target="_blank">Download original</a> · ${a.width}×${a.height}${a.duration ? " · " + a.duration.toFixed(1) + "s" : ""} · ${(a.size_bytes / 1e6).toFixed(1)} MB</p>
    <h3>Account</h3><div class="row"><select id="assignAcct">${state.accounts.map((x) => `<option value="${x.id}" ${x.id === a.account_id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>
      <input id="assignCampaign" placeholder="Campaign" value="${esc(a.campaign)}"><button class="btn" id="assignBtn">${a.account_id ? "Reassign" : "Assign"}</button>
      <span class="small muted">via ${esc(a.assignment_source || "—")}</span></div>
    <h3>Content rights</h3><div class="row"><label><input type="checkbox" id="rightsChk" ${a.rights_confirmed ? "checked" : ""}> I own this content or have permission to publish it</label><button class="btn" id="rightsBtn">Save</button></div>
    <p class="small muted">${esc(a.rights_note)}</p>
    <h3>AI analysis <span class="badge">${esc(a.analysis_source || "pending")}</span></h3>
    ${ai ? `<p>${esc(ai.summary)}</p><p class="small">Pillar: <b>${esc(ai.suggested_pillar)}</b> · quality ${ai.visual_quality}/100 · suitability ${ai.instagram_suitability}/100 · mood ${esc(ai.mood)}</p>
      ${ai.sensitive_flags.length ? `<div class="alert warn" style="margin:6px 0">Flagged: ${esc(ai.sensitive_flags.join(", "))}</div>` : ""}` : `<p class="muted small">${esc(a.analysis?.ai_unavailable_reason || "Not analysed yet.")}</p>`}
    ${a.review_reasons.length ? `<ul class="small">${a.review_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    <h3>Edits applied</h3>${a.derivatives.filter((d) => d.operations.length && d.kind !== "preview").map((d) => `<p class="small"><b>${esc(d.kind)}</b>: ${d.operations.map((o) => esc(o.op + (o.reason ? ` (${o.reason})` : ""))).join(" → ")}</p>`).join("") || "<p class='muted small'>None</p>"}
    <h3>Measured image statistics</h3><pre class="json">${esc(JSON.stringify(a.analysis?.stats || {}, null, 1))}</pre>
    <h3>History</h3><ul class="list small">${a.events.map((e) => `<li>${fmtDate(e.ts)} · ${esc(e.action)}</li>`).join("")}</ul>
    <button class="btn" id="reprocBtn">Re-run analysis & editing</button>`);
  $("#assignBtn").onclick = () => act(() => api(`/api/assets/${id}/assign`, { method: "POST", body: { account_id: Number($("#assignAcct").value), campaign: $("#assignCampaign").value } }), "Assigned — processing queued").then(route);
  $("#rightsBtn").onclick = () => act(() => api(`/api/assets/${id}/rights`, { method: "POST", body: { confirmed: $("#rightsChk").checked } }), "Saved");
  $("#reprocBtn").onclick = () => act(() => api(`/api/assets/${id}/reprocess`, { method: "POST" }), "Reprocessing queued");
};

// ------------------------------------------------------------------ studio
VIEWS.studio = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const posts = await api(`/api/posts?account_id=${state.accountId}&status=needs_review,draft,approved`);
  v.innerHTML = `<h1>AI Content Studio</h1><p class="sub">Review AI-prepared posts. Only exceptions need you once automation rules are set.</p>
    ${posts.length ? `<div class="table-wrap card"><table><tr><th></th><th>Post</th><th>Format</th><th>Pillar</th><th>QC</th><th>Status</th><th>Why it needs you</th></tr>
    ${posts.map((p) => `<tr class="click" onclick="postDetail(${p.id})"><td>${p.preview_url ? `<img src="${p.preview_url}" width="56" height="56" style="object-fit:cover;border-radius:6px" alt="">` : ""}</td>
      <td><b>${esc(p.hook || "(no hook)")}</b><div class="small muted">${esc(p.caption.slice(0, 90))}</div></td><td>${esc(p.format)}</td><td>${esc(p.pillar)}</td>
      <td class="num">${p.qc_score ?? "—"}</td><td>${statusBadge(p.status)}</td><td class="small">${esc((p.approval_reasons || []).slice(0, 2).join("; "))}</td></tr>`).join("")}</table></div>`
      : empty("Nothing waiting", "New posts appear here after uploads are analysed and edited.")}`;
  const open = params().get("post"); if (open) setTimeout(() => postDetail(Number(open)), 0);
};

window.postDetail = async (id) => {
  const p = await api(`/api/posts/${id}`);
  const editable = !["published", "publishing"].includes(p.status);
  const qc = p.qc?.checks || [];
  openDrawer(`<h1>Post #${p.id} ${statusBadge(p.status)}</h1><p class="small muted">${esc(p.format)} · ${esc(p.pillar)} · copy: ${esc(p.content_source)} ${p.approved_by ? "· approved by " + esc(p.approved_by) : ""}</p>
    <div class="grid g2">${p.media.map((m) => m.export ? (m.export.kind.startsWith("reel") ? `<video src="${m.export.url}" controls style="width:100%;border-radius:8px" ${m.cover ? `poster="${m.cover.url}"` : ""}></video>` : `<img src="${m.export.url}" style="width:100%;border-radius:8px" alt="">`) : "").join("")}</div>
    ${p.approval_reasons.length ? `<div class="alert warn" style="margin:12px 0"><b>${p.approved_by ? "Flagged for review (approved anyway by " + esc(p.approved_by) + "):" : "Needs approval because:"}</b><ul>${p.approval_reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>` : ""}
    ${p.last_error ? `<div class="alert bad" style="margin:12px 0">${esc(p.last_error)}</div>` : ""}
    <form id="postForm">
      <label class="field"><span>Hook</span><input name="hook" value="${esc(p.hook)}" ${editable ? "" : "disabled"}></label>
      <label class="field"><span>Caption</span><textarea name="caption" rows="7" ${editable ? "" : "disabled"}>${esc(p.caption)}</textarea></label>
      <label class="field"><span>Hashtags (space separated)</span><input name="hashtags" value="${esc((p.hashtags || []).join(" "))}" ${editable ? "" : "disabled"}></label>
      <label class="field"><span>Search keywords <span class="muted">(AI suggestions, not verified search data)</span></span><input name="keywords" value="${esc((p.keywords || []).join(", "))}" ${editable ? "" : "disabled"}></label>
      <div class="grid g2"><label class="field"><span>Cover title</span><input name="cover_title" value="${esc(p.cover_title)}" ${editable ? "" : "disabled"}></label>
      <label class="field"><span>On-screen text (Reels)</span><input name="on_screen_text" value="${esc(p.on_screen_text)}" ${editable ? "" : "disabled"}></label></div>
      <label class="field"><span>Alt text</span><input name="alt_text" value="${esc(p.alt_text)}" ${editable ? "" : "disabled"}></label>
      ${editable ? `<button class="btn">Save edits</button>` : ""}
    </form>
    <p class="small muted">Full caption: ${p.full_caption.length}/2200 characters</p>
    ${p.variants.length ? `<h3>Creative angles</h3><ul class="list">${p.variants.map((x, i) => `<li><b>${esc(x.angle_name)}</b> — ${esc(x.hook)}<div class="small muted">${esc(x.rationale)}</div>
      ${editable ? `<button class="btn small" onclick="useVariant(${p.id},${i})">Use this angle</button>` : ""}</li>`).join("")}</ul><p class="small">Selection reason: ${esc(p.qc?.selection_reason || "—")}</p>` : ""}
    <h3>Quality checks · score ${p.qc?.score ?? "—"}</h3><ul class="checks small">${qc.map((c) => `<li>${c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✕"} <b>${esc(c.check)}</b> ${esc(c.detail)}</li>`).join("")}</ul>
    ${p.risk_flags.length ? `<p class="small"><b>Risk flags:</b> ${esc(p.risk_flags.join("; "))}</p>` : ""}
    <div class="row" style="margin-top:14px">
      ${["needs_review", "draft"].includes(p.status) ? `<button class="btn primary" onclick="postAction(${p.id},'approve','Approved')">Approve</button>` : ""}
      ${p.status === "approved" ? `<button class="btn primary" onclick="postAction(${p.id},'schedule','Scheduled for the next best slot',{})">Schedule (best slot)</button>
        <input type="datetime-local" id="whenIn"><button class="btn" onclick="scheduleAt(${p.id})">Schedule at time</button>
        <button class="btn" onclick="postAction(${p.id},'publish-now','Queued to publish now')">Publish now</button>` : ""}
      ${p.status === "scheduled" ? `<button class="btn" onclick="postAction(${p.id},'unschedule','Unscheduled')">Unschedule</button>` : ""}
      ${p.status === "failed" ? `<button class="btn primary" onclick="postAction(${p.id},'retry','Retry queued')">Retry publishing</button>` : ""}
      ${editable ? `<button class="btn" onclick="postAction(${p.id},'regenerate','Copy regenerated')">Regenerate copy</button>
        <button class="btn danger-outline" onclick="postAction(${p.id},'reject','Cancelled')">Cancel post</button>` : ""}
      ${p.permalink ? `<a class="btn" href="${esc(p.permalink)}" target="_blank" rel="noopener">View on Instagram</a>` : ""}
    </div>
    <h3>Edit trail</h3>${p.media.map((m) => `<p class="small"><a href="${m.original_url}" target="_blank">Original ${esc(m.filename)}</a>: ${(m.edit_operations || []).map((o) => esc(o.op)).join(" → ") || "no edits"}</p>`).join("")}
    <h3>History</h3><ul class="list small">${p.events.map((e) => `<li>${fmtDate(e.ts)} · ${esc(e.action)} · ${esc(e.actor)}</li>`).join("")}</ul>`);
  const form = $("#postForm");
  if (form) form.onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(form);
    await act(() => api(`/api/posts/${id}`, { method: "PATCH", body: { hook: f.get("hook"), caption: f.get("caption"),
      hashtags: f.get("hashtags").split(/\s+/).map((x) => x.replace(/^#/, "")).filter(Boolean), keywords: f.get("keywords").split(",").map((x) => x.trim()).filter(Boolean),
      cover_title: f.get("cover_title"), on_screen_text: f.get("on_screen_text"), alt_text: f.get("alt_text") } }), "Saved — review re-run");
    postDetail(id);
  };
};
window.postAction = async (id, action, msg, body) => { await act(() => api(`/api/posts/${id}/${action}`, { method: "POST", body: body ?? {} }), msg); await route(); postDetail(id); };
window.useVariant = async (id, i) => { await act(() => api(`/api/posts/${id}/variant/${i}`, { method: "POST" }), "Angle applied"); postDetail(id); };
window.scheduleAt = async (id) => { const v = $("#whenIn").value; if (!v) return toast("Pick a date and time"); await postAction(id, "schedule", "Scheduled", { when: new Date(v).toISOString() }); };

// ---------------------------------------------------------------- calendar
VIEWS.calendar = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const start = new Date(today); start.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  const end = new Date(start); end.setDate(end.getDate() + 28);
  const iso = (d) => d.toISOString().slice(0, 10);
  const [posts, weekly, monthly] = await Promise.all([
    api(`/api/calendar?account_id=${state.accountId}&start=${iso(start)}&end=${iso(end)}`),
    api(`/api/plans?account_id=${state.accountId}&kind=week`), api(`/api/plans?account_id=${state.accountId}&kind=month`)]);
  const days = [...Array(28)].map((_, i) => { const d = new Date(start); d.setDate(d.getDate() + i); return d; });
  const on = (d, p) => { const t = new Date((p.scheduled_for || p.published_at) + "Z"); return t.toDateString() === d.toDateString(); };
  const wk = weekly[0]?.plan, mo = monthly[0];
  v.innerHTML = `<div class="row"><div><h1>Content Calendar</h1><p class="sub">Times shown in your browser's time zone. Account time zone: ${esc(acct().timezone)}.</p></div><div class="grow"></div>
    <button class="btn" id="mkWeek">Generate weekly plan</button><button class="btn" id="mkMonth">Generate monthly strategy</button></div>
    <div class="cal">${["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="small muted" style="padding:0 6px">${d}</div>`).join("")}
      ${days.map((d) => `<div class="day ${d.getTime() === today.getTime() ? "today" : ""}"><div class="d">${d.getDate()} ${d.toLocaleString(undefined, { month: "short" })}</div>
      ${posts.filter((p) => on(d, p)).map((p) => `<div class="item" onclick="postDetail(${p.id})">${new Date((p.scheduled_for || p.published_at) + "Z").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} ${statusBadge(p.status)}<div>${esc(p.format)} · ${esc((p.hook || "").slice(0, 40))}</div></div>`).join("")}</div>`).join("")}</div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h2>Weekly plan ${wk ? `<span class="muted small">week of ${esc(weekly[0].start_date)}</span>` : ""}</h2>
        ${wk ? `<p class="small muted">Timing: ${esc(wk.timing.note)}</p><table><tr><th>When</th><th>Format</th><th>Pillar</th><th>Hook / gap</th><th></th></tr>
        ${wk.entries.map((e) => `<tr><td class="small">${fmtDate(e.when)}</td><td>${esc(e.format || "—")}</td><td>${esc(e.pillar)}</td><td class="small">${e.status === "content_gap" ? `<span class="badge warn">content gap</span> ${esc(e.message)}` : esc(e.hook)}</td>
          <td>${e.post_id ? statusBadge(e.status) : ""}</td></tr>`).join("")}</table>
        <p class="small muted">Pillar weights (adjusted by measured performance): ${esc(JSON.stringify(wk.pillar_weights))}</p>
        <button class="btn primary" id="applyWeek">Schedule approved posts from this plan</button>` : empty("No weekly plan yet", "Generate one to slot ready content into the best times.")}</div>
      <div class="card"><h2>Monthly strategy ${mo ? `<span class="badge">${esc(mo.source)}</span>` : ""}</h2>${mo ? monthBlock(mo.plan.strategy) : empty("No monthly strategy yet", "Generated from your measured performance.")}</div></div>`;
  $("#mkWeek").onclick = async () => { await act(() => api("/api/plans/weekly", { method: "POST", body: { account_id: state.accountId } }), "Weekly plan created"); route(); };
  $("#mkMonth").onclick = async () => { await act(() => api("/api/plans/monthly", { method: "POST", body: { account_id: state.accountId } }), "Monthly strategy created"); route(); };
  if (wk) $("#applyWeek").onclick = async () => { const r = await act(() => api(`/api/plans/${weekly[0].id}/apply`, { method: "POST" })); toast(`${r.scheduled.length} post(s) scheduled`); route(); };
};
const monthBlock = (s) => `<p>${esc(s.rationale)}</p>${[["Campaign themes", s.campaign_themes], ["Weekly focus", s.weekly_focus], ["Key dates", s.key_dates], ["Growth targets", s.growth_targets],
  ["Testing priorities", s.testing_priorities], ["Production needs", s.production_needs], ["Repurposing", s.repurposing]].map(([t, l]) => `<h3>${t}</h3><ul class="small">${(l || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li class='muted'>—</li>"}</ul>`).join("")}
  <h3>Format mix</h3><p class="small">${esc(Object.entries(s.format_mix || {}).map(([k, n]) => `${n} ${k}`).join(" · "))}</p><h3>Pillar allocation</h3><p class="small">${esc(Object.entries(s.pillar_allocation || {}).map(([k, n]) => `${k} ${n}%`).join(" · "))}</p>
  <p class="small"><b>Brand vs conversion:</b> ${esc(s.brand_vs_conversion)}</p>`;

// ------------------------------------------------------------------- queue
VIEWS.queue = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const [posts, auto] = await Promise.all([api(`/api/posts?account_id=${state.accountId}&status=scheduled,publishing,failed,published&limit=100`), api("/api/automation")]);
  const group = (s) => posts.filter((p) => s.includes(p.status));
  const table = (list, cols) => list.length ? `<div class="table-wrap"><table><tr><th></th><th>Post</th><th>Format</th><th>${cols}</th><th>Status</th><th></th></tr>${list.map((p) => `<tr class="click" onclick="postDetail(${p.id})">
    <td>${p.preview_url ? `<img src="${p.preview_url}" width="44" height="44" style="object-fit:cover;border-radius:6px" alt="">` : ""}</td><td><b>${esc((p.hook || p.caption).slice(0, 60))}</b>${p.last_error ? `<div class="small" style="color:var(--bad)">${esc(p.last_error)}</div>` : ""}</td>
    <td>${esc(p.format)}</td><td class="small">${fmtDate(p.published_at || p.scheduled_for)}</td><td>${statusBadge(p.status)}</td><td>${p.permalink ? `<a href="${esc(p.permalink)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open ↗</a>` : ""}</td></tr>`).join("")}</table></div>` : `<p class="muted">None.</p>`;
  v.innerHTML = `<h1>Publishing Queue</h1><p class="sub">A post is marked published only after Instagram confirms it.</p>
    ${auto.publishing_paused ? `<div class="alert bad" style="margin:0 0 14px">Publishing is paused for every account. Nothing new will be sent to Instagram until you resume.</div>` : ""}
    ${acct().publishing_paused ? `<div class="alert warn" style="margin:0 0 14px">Publishing is paused for this account (Automation Controls).</div>` : ""}
    <div class="card"><h2>Upcoming</h2>${table(group(["scheduled", "publishing"]), "Scheduled for")}</div>
    <div class="card" style="margin-top:14px"><h2>Failed — needs attention</h2>${table(group(["failed"]), "Was scheduled")}</div>
    <div class="card" style="margin-top:14px"><h2>Published</h2>${table(group(["published"]), "Published at")}</div>`;
};

// --------------------------------------------------------------- analytics
VIEWS.analytics = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const [rows, avail, defs] = await Promise.all([api(`/api/analytics/content?account_id=${state.accountId}`), api(`/api/analytics/availability?account_id=${state.accountId}`), api("/api/analytics/definitions")]);
  const p = params(); const sort = p.get("sort") || "posted_at"; const fmt = p.get("format") || ""; const dir = p.get("dir") === "asc" ? 1 : -1;
  const get = (r, k) => (k === "posted_at" ? r.posted_at : k === "er_reach" ? r.er_reach : k === "relative_reach" ? r.relative_reach : r.metrics[k]) ?? -Infinity;
  const list = rows.filter((r) => !fmt || r.format === fmt).sort((a, b) => (get(a, sort) > get(b, sort) ? dir : -dir));
  const th = (k, label) => `<th class="sortable num" onclick="location.hash='#/analytics?sort=${k}&dir=${sort === k && dir === -1 ? "asc" : "desc"}&format=${fmt}'">${label}${sort === k ? (dir === -1 ? " ↓" : " ↑") : ""}</th>`;
  const formats = [...new Set(rows.map((r) => r.format))];
  v.innerHTML = `<h1>Analytics</h1><p class="sub">Per-post performance from Instagram insights. Relative performance compares each post with the median of the previous 20 posts of the same format.</p>
    <div class="tabs"><button class="${!fmt ? "on" : ""}" onclick="location.hash='#/analytics?sort=${sort}'">All formats</button>${formats.map((f) => `<button class="${f === fmt ? "on" : ""}" onclick="location.hash='#/analytics?sort=${sort}&format=${f}'">${esc(f)}</button>`).join("")}</div>
    <div class="card table-wrap">${list.length ? `<table><tr><th></th><th class="sortable" onclick="location.hash='#/analytics?sort=posted_at&format=${fmt}'">Date</th><th>Format</th><th>Caption</th>
      ${th("reach", "Reach")}${th("views", "Views")}${th("likes", "Likes")}${th("comments", "Comm.")}${th("shares", "Shares")}${th("saved", "Saves")}${th("follows", "Follows")}${th("ig_reels_avg_watch_time", "Avg watch (s)")}${th("er_reach", "ER")}${th("relative_reach", "vs baseline")}</tr>
      ${list.map((r) => `<tr class="click" onclick="mediaHistory(${r.id})"><td>${r.thumbnail_url ? `<img src="${esc(r.thumbnail_url)}" width="40" height="40" style="object-fit:cover;border-radius:6px" alt="" referrerpolicy="no-referrer">` : ""}</td>
        <td class="small">${fmtDay(r.posted_at)}</td><td>${esc(r.format)}</td><td class="small">${esc(r.caption.slice(0, 60))}</td>
        ${["reach", "views", "likes", "comments", "shares", "saved", "follows"].map((k) => `<td class="num">${fmtN(r.metrics[k])}</td>`).join("")}
        <td class="num">${r.metrics.ig_reels_avg_watch_time != null ? (r.metrics.ig_reels_avg_watch_time / 1000).toFixed(1) : "—"}</td><td class="num">${fmtPct(r.er_reach)}</td>
        <td class="num">${r.relative_reach != null ? `<span class="badge ${r.relative_reach >= 1.2 ? "good" : r.relative_reach < 0.8 ? "warn" : ""}">${r.relative_reach}×</span>` : "—"}</td></tr>`).join("")}</table>`
      : empty("No posts synced yet", "Connect the account on the Integrations page and run a sync.")}</div>
    <p class="small muted">— means the metric was not returned for that post (for example watch time only exists for Reels).</p>
    <div class="grid g2" style="margin-top:14px"><div class="card"><h2>Metric availability from the connected source</h2>${avail.length ? `<table><tr><th>Scope</th><th>Metric</th><th>Status</th></tr>${avail.map((a) => `<tr><td>${esc(a.scope)}</td><td>${esc(a.metric)}</td><td>${a.available ? "<span class='badge good'>✓ available</span>" : `<span class="badge warn">unavailable</span><div class="small muted">${esc(a.message)}</div>`}</td></tr>`).join("")}</table>` : `<p class="muted">Checked during the first sync.</p>`}</div>
    <div class="card"><h2>Metric definitions</h2><ul class="list small">${Object.entries(defs).map(([k, d]) => `<li><b>${esc(k)}</b>: ${esc(d)}</li>`).join("")}</ul></div></div>`;
};
window.mediaHistory = async (id) => {
  const h = await api(`/api/analytics/content/${id}`);
  openDrawer(`<h1>Post performance</h1><p class="small">${esc(h.format)} · ${fmtDate(h.posted_at)} ${h.permalink ? `· <a href="${esc(h.permalink)}" target="_blank" rel="noopener">Instagram ↗</a>` : ""}</p><p>${esc(h.caption)}</p>
    <h3>Snapshots</h3><table><tr><th>Captured</th><th class="num">Reach</th><th class="num">Views</th><th class="num">Interactions</th><th class="num">Saves</th><th class="num">Shares</th></tr>
    ${h.history.map((x) => `<tr><td class="small">${fmtDate(x.captured_at)}</td><td class="num">${fmtN(x.metrics.reach)}</td><td class="num">${fmtN(x.metrics.views)}</td><td class="num">${fmtN(x.metrics.total_interactions)}</td><td class="num">${fmtN(x.metrics.saved)}</td><td class="num">${fmtN(x.metrics.shares)}</td></tr>`).join("")}</table>
    ${h.history.length > 1 ? `<div class="chart-box" style="margin-top:12px"><canvas id="hChart"></canvas></div>` : ""}${h.post_id ? `<p><a href="#/studio?post=${h.post_id}" onclick="closeDrawer()">Open the post record</a></p>` : ""}`);
  if (h.history.length > 1) lineChart($("#hChart"), h.history.map((x) => ({ day: fmtDay(x.captured_at), value: x.metrics.reach })), "Reach");
};

// ---------------------------------------------------------------- audience
VIEWS.audience = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const a = await api(`/api/analytics/audience?account_id=${state.accountId}`);
  const block = (metric, bd, title) => {
    const e = a[metric][bd];
    if (!e.available) return `<div class="card"><h2>${title}</h2><p class="muted">${esc(e.message)}</p></div>`;
    const total = Object.values(e.data).reduce((x, y) => x + y, 0);
    return `<div class="card"><h2>${title}</h2>${bars(e.data, total)}<p class="small muted">As reported by Instagram · captured ${fmtDate(e.captured_at)}</p>
      ${e.change ? `<p class="small">Change since ${fmtDay(e.previous_captured_at)}: ${esc(Object.entries(e.change).filter(([, d]) => d).slice(0, 6).map(([k, d]) => `${k} ${d > 0 ? "+" : ""}${d}`).join(", ") || "no change")}</p>` : ""}</div>`;
  };
  v.innerHTML = `<h1>Audience Insights</h1><p class="sub">${esc(a.policy)}</p>
    <div class="tabs"><button class="on" id="tF">Followers</button><button id="tE">Engaged audience</button></div><div id="audBody"></div>
    <div class="card" style="margin-top:14px"><h2>Current vs target audience</h2>${a.target_comparison.note ? `<p class="muted">${esc(a.target_comparison.note)}</p>` :
      `<table><tr><th>Target country</th><th class="num">Share of followers</th></tr>${Object.entries(a.target_comparison.countries).map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${x}%</td></tr>`).join("")}</table>`}</div>`;
  const show = (metric) => { $("#audBody").innerHTML = `<div class="grid g2">${block(metric, "country", "Countries")}${block(metric, "city", "Cities")}${block(metric, "age", "Age ranges")}${block(metric, "gender", "Gender (Instagram's reported categories)")}</div>`;
    $("#tF").classList.toggle("on", metric === "follower_demographics"); $("#tE").classList.toggle("on", metric !== "follower_demographics"); };
  $("#tF").onclick = () => show("follower_demographics"); $("#tE").onclick = () => show("engaged_audience_demographics");
  show("follower_demographics");
};

// ------------------------------------------------------------- experiments
VIEWS.experiments = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const [exps, g] = await Promise.all([api(`/api/experiments?account_id=${state.accountId}`), api(`/api/analytics/growth?account_id=${state.accountId}&days=30`)]);
  v.innerHTML = `<h1>Growth Experiments</h1><p class="sub">Change one variable at a time. Tag posts to an experiment from the post's ID below, then evaluate.</p>
    <div class="grid g2"><div class="card"><h2>Suggested hypotheses</h2>${g.insights.hypotheses?.length ? `<ul class="list">${g.insights.hypotheses.map((h, i) => `<li>${esc(h.hypothesis)} <span class="badge">${esc(h.confidence)}</span>
      <div class="small muted">${esc(h.note || "")}</div><button class="btn" onclick='newExp(${JSON.stringify({ hypothesis: h.hypothesis, variable: h.test.variable, target_metric: h.test.target_metric }).replace(/'/g, "&#39;")})'>Start this experiment</button></li>`).join("")}</ul>` : `<p class="muted">Hypotheses appear once there is enough performance data.</p>`}</div>
    <div class="card"><h2>New experiment</h2><form id="expForm"><label class="field"><span>Hypothesis</span><input name="hypothesis" required></label>
      <div class="grid g2"><label class="field"><span>Variable (one only)</span><input name="variable" required placeholder="hook_style"></label><label class="field"><span>Target metric</span><select name="target_metric">${["reach", "views", "saved", "shares", "follows", "total_interactions"].map((m) => `<option>${m}</option>`).join("")}</select></label></div>
      <button class="btn primary">Create</button></form></div></div>
    <div class="card table-wrap" style="margin-top:14px"><h2>Experiment register</h2>${exps.length ? `<table><tr><th>#</th><th>Hypothesis</th><th>Variable</th><th>Metric</th><th class="num">Baseline</th><th class="num">Result</th><th>Confidence & limitations</th><th>Decision</th><th></th></tr>
      ${exps.map((e) => `<tr><td>${e.id}</td><td>${esc(e.hypothesis)}</td><td>${esc(e.variable)}</td><td>${esc(e.target_metric)}</td><td class="num">${fmtN(e.baseline)}</td><td class="num">${fmtN(e.result)}${e.result_detail?.lift != null ? ` (${(e.result_detail.lift * 100).toFixed(0)}%)` : ""}</td>
        <td class="small">${esc(e.confidence)} — ${esc(e.limitations)}</td><td>${e.decision ? `<span class="badge">${esc(e.decision.replace("_", " "))}</span>` : statusBadge(e.status)}</td><td><button class="btn" onclick="evalExp(${e.id})">Evaluate</button></td></tr>`).join("")}</table>
      <h3>Tag a post</h3><div class="row"><input id="tagPost" placeholder="Post #" size="6"><select id="tagExp">${exps.map((e) => `<option value="${e.id}">#${e.id} ${esc(e.variable)}</option>`).join("")}</select><select id="tagArm"><option>test</option><option>control</option></select><button class="btn" id="tagBtn">Tag</button></div>` : `<p class="muted">No experiments yet.</p>`}</div>`;
  $("#expForm").onsubmit = (e) => { e.preventDefault(); const f = new FormData(e.target); newExp(Object.fromEntries(f)); };
  const tb = $("#tagBtn"); if (tb) tb.onclick = () => act(() => api(`/api/posts/${$("#tagPost").value}/experiment`, { method: "POST", body: { experiment_id: Number($("#tagExp").value), arm: $("#tagArm").value } }), "Post tagged");
};
window.newExp = async (body) => { await act(() => api("/api/experiments", { method: "POST", body: { ...body, account_id: state.accountId } }), "Experiment created"); route(); };
window.evalExp = async (id) => { await act(() => api(`/api/experiments/${id}/evaluate`, { method: "POST" }), "Evaluated"); route(); };

// --------------------------------------------------------------- community
VIEWS.community = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const cat = params().get("category") || "";
  const [d, tpl] = await Promise.all([api(`/api/community?account_id=${state.accountId}${cat ? "&category=" + cat : ""}`), api(`/api/reply-templates?account_id=${state.accountId}`)]);
  const s = d.summary;
  v.innerHTML = `<h1>Community</h1><p class="sub">Comments on your own posts. Sensitive, negative and urgent items are never answered automatically.</p>
    <div class="kpis">${[["Comments (7d)", s.total], ["Escalated", s.escalated], ["Possible leads", s.leads], ["Replied", s.replied]].map(([l, x]) => `<div class="card kpi"><div class="label">${l}</div><div class="value">${fmtN(x)}</div></div>`).join("")}</div>
    <div class="tabs">${["", "complaint", "lead", "question", "praise", "spam", "other"].map((c) => `<button class="${c === cat ? "on" : ""}" onclick="location.hash='#/community?category=${c}'">${c || "All"}</button>`).join("")}</div>
    <div class="card">${d.items.length ? `<ul class="list">${d.items.map((c) => `<li><div class="row"><b>@${esc(c.username)}</b><span class="badge ${c.category === "complaint" ? "danger" : c.category === "lead" ? "good" : ""}">${esc(c.category)}</span>
      ${c.urgent ? "<span class='badge danger'>! urgent</span>" : ""}${c.escalated ? "<span class='badge warn'>escalated</span>" : ""}<span class="muted small">${fmtDate(c.commented_at)} · ${esc(c.classification_source)}</span>${statusBadge(c.reply_status)}</div>
      <p>${esc(c.text)}</p>${c.reply_status !== "sent" && c.category !== "spam" ? `<div class="row"><input id="r${c.id}" value="${esc(c.draft_reply)}" placeholder="Write a reply" style="flex:1;min-width:200px">
      <button class="btn primary" onclick="sendReply(${c.id})">Send reply</button><button class="btn ghost" onclick="skipC(${c.id})">Skip</button></div>${c.reply_error ? `<p class="small" style="color:var(--bad)">${esc(c.reply_error)}</p>` : ""}` : c.reply_status === "sent" ? `<p class="small muted">Replied: ${esc(c.draft_reply)}</p>` : ""}</li>`).join("")}</ul>`
      : empty("No comments yet", "Comments are synced hourly for posts from the last 30 days.")}</div>
    <div class="grid g2" style="margin-top:14px"><div class="card"><h2>Frequent question terms</h2><p>${esc(s.frequent_question_terms.map(([w, n]) => `${w} (${n})`).join(", ") || "—")}</p></div>
    <div class="card"><h2>Reply templates</h2><ul class="list">${tpl.map((t) => `<li><b>${esc(t.name)}</b> · ${esc(t.category)} ${t.auto_send ? "<span class='badge info'>auto-send</span>" : ""}<div class="small">${esc(t.text)}</div><button class="btn ghost small" onclick="delTpl(${t.id})">Delete</button></li>`).join("") || "<li class='muted'>None</li>"}</ul>
      <form id="tplForm" class="row"><input name="name" placeholder="Name" required><select name="category">${["praise", "question", "lead", "complaint"].map((c) => `<option>${c}</option>`).join("")}</select>
      <input name="text" placeholder="Reply text" required style="flex:1"><label class="small"><input type="checkbox" name="auto_send"> auto-send (praise/question only)</label><button class="btn">Add</button></form></div></div>`;
  $("#tplForm").onsubmit = async (e) => { e.preventDefault(); const f = new FormData(e.target);
    await act(() => api("/api/reply-templates", { method: "POST", body: { account_id: state.accountId, name: f.get("name"), category: f.get("category"), text: f.get("text"), auto_send: !!f.get("auto_send") } }), "Template added"); route(); };
};
window.sendReply = async (id) => { await act(() => api(`/api/community/${id}/reply`, { method: "POST", body: { text: $("#r" + id).value } }), "Reply sent"); route(); };
window.skipC = async (id) => { await api(`/api/community/${id}/skip`, { method: "POST" }); route(); };
window.delTpl = async (id) => { await api(`/api/reply-templates/${id}`, { method: "DELETE" }); route(); };

// ----------------------------------------------------------------- reports
VIEWS.reports = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const list = (await api(`/api/reports?account_id=${state.accountId}`)).filter((r) => r.kind !== "audit");
  const open = Number(params().get("id")) || list[0]?.id;
  const r = open ? await api(`/api/reports/${open}`) : null;
  v.innerHTML = `<div class="row"><div><h1>Reports</h1><p class="sub">Weekly reports arrive every Monday, monthly reviews on the 1st.</p></div><div class="grow"></div>
    <button class="btn" onclick="mkReport('weekly')">Generate weekly</button><button class="btn" onclick="mkReport('monthly')">Generate monthly</button></div>
    <div class="grid" style="grid-template-columns:minmax(0,220px) minmax(0,1fr)"><div class="card"><ul class="list">${list.map((x) => `<li><a href="#/reports?id=${x.id}">${esc(x.kind)} · ${esc(x.period_start)} → ${esc(x.period_end)}</a></li>`).join("") || "<li class='muted'>No reports yet</li>"}</ul></div>
    <div>${r ? reportBlock(r) : empty("No report selected", "")}</div></div>`;
};
window.mkReport = async (kind) => { const r = await act(() => api("/api/reports", { method: "POST", body: { account_id: state.accountId, kind } }), "Report generated"); location.hash = `#/reports?id=${r.id}`; };
function reportBlock(r) {
  const d = r.data;
  return `<div class="card"><div class="row"><h2 style="margin:0">${esc(d.kind)} report · ${esc(d.period.start)} → ${esc(d.period.end)}</h2><div class="grow"></div>
    <a class="btn" href="/api/reports/${r.id}/export.csv">CSV</a><a class="btn" href="/api/reports/${r.id}/export.pdf">PDF</a></div>
    ${d.narrative ? `<p>${esc(d.narrative.summary)}</p>` : `<p class="small muted">Narrative ${esc(d.narrative_source)}.</p>`}
    <table><tr><th>Metric</th><th class="num">This period</th><th class="num">Previous</th><th class="num">Change</th></tr>${Object.entries(d.comparisons).map(([k, x]) => `<tr><td>${esc(k.replace(/_/g, " "))}</td><td class="num">${fmtN(x.current)}</td><td class="num">${fmtN(x.previous)}</td><td class="num">${x.change_pct == null ? "n/a" : (x.change_pct > 0 ? "▲ " : "▼ ") + Math.abs(x.change_pct) + "%"}</td></tr>`).join("")}</table>
    <p class="small muted">n/a = not available from the connected source for one of the periods.</p>
    <h3>Publishing consistency</h3><p>${d.publishing.published} published of ${d.publishing.planned} planned · ${d.publishing.failed} failed · target ${d.publishing.target_per_week}/week</p>
    <h3>Best content</h3><ul class="small">${d.best_content.map((x) => `<li>${fmtN(x.metrics.reach)} reach · ${esc(x.format)} · ${esc(x.caption.slice(0, 80))}</li>`).join("") || "<li>—</li>"}</ul>
    <h3>Weakest content</h3><ul class="small">${d.weakest_content.map((x) => `<li>${fmtN(x.metrics.reach)} reach · ${esc(x.format)} · ${esc(x.caption.slice(0, 80))}</li>`).join("") || "<li>—</li>"}</ul>
    ${growthBlock(d.growth)}
    ${d.narrative?.problems?.length ? `<h3>Problems requiring attention</h3><ul>${d.narrative.problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    ${d.narrative?.next_actions?.length ? `<h3>Next actions</h3><ul>${d.narrative.next_actions.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
    <h3>Experiments</h3><ul class="small">${d.experiments.map((e) => `<li>${esc(e.variable)}: ${esc(e.decision || "running")} (${esc(e.confidence || "—")})</li>`).join("") || "<li>None</li>"}</ul>
    <h3>Community</h3><p class="small">${d.community.total} comments · ${d.community.leads} leads · ${d.community.escalated} escalated</p>
    ${d.next_month_plan ? `<h3>Next month</h3>${monthBlock(d.next_month_plan.strategy)}` : ""}</div>`;
}

// ------------------------------------------------------------------- brand
VIEWS.brand = async (v) => {
  const na = needAccount(); if (na) return (v.innerHTML = na);
  const a = await api(`/api/accounts/${state.accountId}`); const b = a.brand; const ta = b.target_audience || {}; const vi = b.visual_identity || {};
  const lines = (x) => esc((x || []).join("\n"));
  v.innerHTML = `<h1>Brand Settings · ${esc(a.name)}</h1><p class="sub">These are the approved brand rules every post must follow. AI suggestions never overwrite them.</p>
    <form id="brandForm" class="grid g2">
      <div class="card"><h2>Positioning</h2>
        <label class="field"><span>Category</span><input name="category" value="${esc(b.category)}"></label>
        <label class="field"><span>Niche</span><input name="niche" value="${esc(b.niche)}"></label>
        <label class="field"><span>Products, services, offers</span><textarea name="offers">${esc(b.offers)}</textarea></label>
        <label class="field"><span>Positioning / value proposition</span><textarea name="positioning">${esc(b.positioning)}</textarea></label>
        <label class="field"><span>Objectives (one per line: followers, reach, engagement, traffic, leads, sales, awareness)</span><textarea name="objectives">${lines(b.objectives)}</textarea></label>
        <label class="field"><span>Business priorities</span><textarea name="priorities">${esc(b.priorities)}</textarea></label></div>
      <div class="card"><h2>Audience & markets</h2>
        <label class="field"><span>Target countries (ISO codes, one per line, e.g. US)</span><textarea name="countries">${lines(ta.countries)}</textarea></label>
        <label class="field"><span>Target cities</span><textarea name="cities">${lines(ta.cities)}</textarea></label>
        <label class="field"><span>Age groups (e.g. 25-34)</span><textarea name="age_groups">${lines(ta.age_groups)}</textarea></label>
        <label class="field"><span>Audience notes</span><textarea name="aud_notes">${esc(ta.notes || "")}</textarea></label>
        <label class="field"><span>Markets</span><textarea name="markets">${lines(b.markets)}</textarea></label>
        <label class="field"><span>Languages</span><textarea name="languages">${lines(b.languages)}</textarea></label></div>
      <div class="card"><h2>Voice & visual identity</h2>
        <label class="field"><span>Brand voice</span><textarea name="voice">${esc(b.voice)}</textarea></label>
        <label class="field"><span>Colours (hex, one per line)</span><textarea name="colors">${lines(vi.colors)}</textarea></label>
        <label class="field"><span>Fonts</span><input name="fonts" value="${esc((vi.fonts || []).join(", "))}"></label>
        <label class="field"><span>Creative references</span><textarea name="references">${lines(vi.references)}</textarea></label>
        <label class="field"><span>Editing preset</span><select name="editing_preset">${["natural", "warm", "cool", "vibrant", "matte", "mono"].map((p) => `<option ${p === b.editing_preset ? "selected" : ""}>${p}</option>`).join("")}</select></label>
        <label class="field"><span>Banned phrases (one per line)</span><textarea name="banned_phrases">${lines(b.banned_phrases)}</textarea></label></div>
      <div class="card"><h2>Content & conversion</h2>
        <label class="field"><span>Content pillars — one per line: Name | purpose | audience | KPI | weight</span><textarea name="pillars" rows="6">${esc((b.content_pillars || []).map((p) => [p.name, p.purpose, p.audience, p.kpi, p.weight].map((x) => x ?? "").join(" | ")).join("\n"))}</textarea></label>
        <label class="field"><span>Approved hashtags (one per line)</span><textarea name="hashtag_bank">${lines(b.hashtag_bank)}</textarea></label>
        <label class="field"><span>Website</span><input name="website" value="${esc(b.website)}"></label>
        <label class="field"><span>Landing / product pages (one per line)</span><textarea name="landing_pages">${lines(b.landing_pages)}</textarea></label>
        <label class="field"><span>Preferred calls to action (one per line)</span><textarea name="ctas">${lines(b.ctas)}</textarea></label>
        <label class="field"><span>Competitors (@handles, one per line)</span><textarea name="competitors">${lines(b.competitors)}</textarea></label>
        <label class="field"><span>Inspirational accounts</span><textarea name="inspirations">${lines(b.inspirations)}</textarea></label></div>
      <div style="grid-column:1/-1" class="row"><label><input type="checkbox" name="onboarding_complete" ${b.onboarding_complete ? "checked" : ""}> Onboarding complete</label><button class="btn primary">Save brand profile</button></div>
    </form>`;
  $("#brandForm").onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(e.target); const L = (k) => (f.get(k) || "").split("\n").map((x) => x.trim()).filter(Boolean);
    const body = { category: f.get("category"), niche: f.get("niche"), offers: f.get("offers"), positioning: f.get("positioning"), objectives: L("objectives"), priorities: f.get("priorities"),
      target_audience: { countries: L("countries").map((x) => x.toUpperCase()), cities: L("cities"), age_groups: L("age_groups"), notes: f.get("aud_notes") }, markets: L("markets"), languages: L("languages"),
      voice: f.get("voice"), visual_identity: { colors: L("colors"), fonts: (f.get("fonts") || "").split(",").map((x) => x.trim()).filter(Boolean), references: L("references") },
      editing_preset: f.get("editing_preset"), banned_phrases: L("banned_phrases"), hashtag_bank: L("hashtag_bank").map((x) => x.replace(/^#/, "")), website: f.get("website"),
      landing_pages: L("landing_pages"), ctas: L("ctas"), competitors: L("competitors"), inspirations: L("inspirations"), onboarding_complete: !!f.get("onboarding_complete"),
      content_pillars: L("pillars").map((line) => { const [name, purpose, audience, kpi, weight] = line.split("|").map((x) => (x || "").trim()); return { name, purpose, audience, kpi, weight: Number(weight) || 1 }; }) };
    await act(() => api(`/api/accounts/${state.accountId}/brand`, { method: "PUT", body }), "Brand profile saved");
  };
};

// ------------------------------------------------------------ integrations
VIEWS.integrations = async (v) => {
  const i = await api("/api/integrations"); const p = params();
  const ok = (b, yes, no) => (b ? `<span class="badge good">✓ ${yes}</span>` : `<span class="badge warn">! ${no}</span>`);
  v.innerHTML = `<h1>Integrations</h1><p class="sub">Accounts connect through Meta's official Instagram Login. Passwords are never requested or stored; tokens are encrypted at rest.</p>
    ${p.get("error") ? `<div class="alert bad" style="margin:0 0 14px">${esc(p.get("error"))}</div>` : ""}${p.get("connected") ? `<div class="alert good" style="margin:0 0 14px">Account connected. The first sync is queued.</div>` : ""}
    <div class="card"><h2>Setup checklist</h2><table>
      <tr><td>Meta app (META_APP_ID / META_APP_SECRET)</td><td>${ok(i.meta_configured, "configured", "not set")}</td><td class="small">Valid OAuth redirect URI to register in the Meta app: <code>${esc(i.redirect_uri)}</code></td></tr>
      <tr><td>Public HTTPS address (PUBLIC_BASE_URL)</td><td>${ok(i.public_media_ready, "ready", "required for publishing")}</td><td class="small">Instagram downloads media from <code>${esc(i.public_base_url)}/public-media/…</code> at publish time.</td></tr>
      <tr><td>Claude (ANTHROPIC_API_KEY)</td><td>${ok(i.anthropic_configured, i.ai_model, "not set — heuristics only, all posts need review")}</td><td></td></tr>
      <tr><td>FFmpeg (video editing)</td><td>${ok(i.ffmpeg, "installed", "missing")}</td><td></td></tr>
      <tr><td>Automatic subtitles (faster-whisper)</td><td>${ok(i.subtitles_engine, "installed", "optional, not installed")}</td><td class="small">Without it, Reels are exported without burned-in subtitles and flagged.</td></tr>
      <tr><td>Watched folder</td><td><span class="badge">${esc(i.watch_root)}</span></td><td class="small">Point Google Drive / Dropbox / OneDrive desktop sync at this folder (or set WATCH_ROOT).</td></tr></table>
      <p class="small muted">Permissions requested: ${esc(i.scopes_requested.join(", "))}</p></div>
    <div class="card" style="margin-top:14px"><h2>Instagram accounts</h2>${i.accounts.length ? `<table><tr><th>Account</th><th>Instagram</th><th>Status</th><th>Token expires</th><th>Last sync</th><th></th></tr>${i.accounts.map((a) => `<tr>
      <td><b>${esc(a.name)}</b><div class="small muted">${esc(a.account_type)} · folder “${esc(a.folder_name)}”</div></td><td>${a.ig_username ? "@" + esc(a.ig_username) : "—"}</td><td>${statusBadge(a.connection_status)}${a.connection_error ? `<div class="small" style="color:var(--bad)">${esc(a.connection_error)}</div>` : ""}</td>
      <td class="small">${fmtDay(a.token_expires_at)}</td><td class="small">${ago(a.insights_synced_at)}</td>
      <td class="row">${a.connection_status === "connected" ? `<button class="btn" onclick="syncAcct(${a.id})">Sync now</button><button class="btn ghost" onclick="disconnect(${a.id})">Disconnect</button>` : `<button class="btn primary" onclick="connectAcct(${a.id})" ${i.meta_configured ? "" : "disabled"}>Connect Instagram</button>`}</td></tr>`).join("")}</table>` : `<p class="muted">No accounts yet.</p>`}
      <h3>Add an account</h3><form id="newAcct" class="row"><input name="name" placeholder="Name, e.g. Creator Account" required><select name="account_type"><option value="creator">Creator</option><option value="business">Business</option></select>
      <input name="timezone" placeholder="Time zone" value="${esc(Intl.DateTimeFormat().resolvedOptions().timeZone)}"><input name="posts_per_week" type="number" min="1" max="21" value="4" style="width:80px"><button class="btn primary">Add account</button></form>
      <p class="small muted">Each account gets its own folder (Name/Inbox) in the watched folder.</p></div>
    <div class="card" style="margin-top:14px"><h2>Website conversions (business accounts)</h2><p class="small">Import CSV with columns <code>date,metric,value,utm_campaign</code> (metrics: sessions, leads, enquiries, purchases, revenue) exported from your analytics tool.</p>
      <form id="convForm" class="row"><input type="file" name="file" accept=".csv" required><input name="method" placeholder="Attribution method, e.g. GA4 utm_source=instagram" required style="flex:1"><button class="btn">Import</button></form></div>`;
  $("#newAcct").onsubmit = async (e) => { e.preventDefault(); const f = new FormData(e.target);
    await act(() => api("/api/accounts", { method: "POST", body: { name: f.get("name"), account_type: f.get("account_type"), timezone: f.get("timezone") || "UTC", posts_per_week: Number(f.get("posts_per_week")) } }), "Account created");
    await loadAccounts(); route(); };
  $("#convForm").onsubmit = async (e) => { e.preventDefault(); const fd = new FormData(e.target); fd.append("account_id", state.accountId);
    const r = await act(() => api("/api/analytics/conversions", { method: "POST", body: fd })); toast(`${r.rows} rows imported`); };
};
window.connectAcct = async (id) => { const r = await act(() => api(`/api/accounts/${id}/connect`, { method: "POST" })); location.href = r.authorize_url; };
window.disconnect = async (id) => { if (!confirm("Disconnect this account? Publishing and syncing stop.")) return; const r = await act(() => api(`/api/accounts/${id}/disconnect`, { method: "POST" })); toast(r.note, 8000); await loadAccounts(); route(); };
window.syncAcct = (id) => act(() => api(`/api/accounts/${id}/sync`, { method: "POST" }), "Sync queued");

// -------------------------------------------------------------- automation
VIEWS.automation = async (v) => {
  const d = await api("/api/automation");
  v.innerHTML = `<h1>Automation Controls</h1><p class="sub">Exceptions (ambiguous assets, sensitive claims, unverified promotions, uncertain rights, failed checks) always need approval, in every mode.</p>
    <div class="card" style="margin-bottom:14px"><div class="row"><div><h2 style="margin:0">Emergency pause</h2><p class="small muted" style="margin:4px 0 0">Stops every new publishing job immediately, across all accounts.</p></div><div class="grow"></div>
      <button class="btn ${d.publishing_paused ? "primary" : "danger"}" id="bigPause">${d.publishing_paused ? "Resume publishing" : "Pause all publishing"}</button></div></div>
    ${d.accounts.map((a) => { const r = a.rules || {}; return `<div class="card" style="margin-bottom:14px"><h2>${esc(a.name)}</h2><form class="autoForm grid g2" data-id="${a.id}">
      <label class="field"><span>Operating mode</span><select name="autonomy_mode">
        <option value="approval" ${a.autonomy_mode === "approval" ? "selected" : ""}>Mode 1 — Approval required for every post</option>
        <option value="limited" ${a.autonomy_mode === "limited" ? "selected" : ""}>Mode 2 — Limited autonomy (auto-publish allowed formats/pillars)</option>
        <option value="full" ${a.autonomy_mode === "full" ? "selected" : ""}>Mode 3 — Full authorised automation</option></select></label>
      <label class="field"><span>Posts per week</span><input name="posts_per_week" type="number" min="1" max="21" value="${a.posts_per_week}"></label>
      <label class="field"><span>Mode 2: formats that may auto-publish</span><input name="auto_formats" value="${esc((r.auto_formats || []).join(", "))}" placeholder="image, carousel, reel"></label>
      <label class="field"><span>Mode 2: pillars that may auto-publish (blank = all)</span><input name="auto_pillars" value="${esc((r.auto_pillars || []).join(", "))}"></label>
      <label class="field"><span>Mode 3: formats that always need approval</span><input name="blocked_auto_formats" value="${esc((r.blocked_auto_formats || []).join(", "))}"></label>
      <label class="field"><span>Minimum quality score to auto-publish</span><input name="min_qc_score" type="number" min="0" max="100" value="${r.min_qc_score ?? 85}"></label>
      <label class="field"><span>Monthly targets (JSON, e.g. {"account_reach": 50000})</span><input name="monthly_targets" value="${esc(JSON.stringify(r.monthly_targets || {}))}"></label>
      <div><label><input type="checkbox" name="uploads_are_owned" ${r.uploads_are_owned ? "checked" : ""}> Everything I upload to this account's Inbox is my own content (confirms rights)</label><br>
        <label><input type="checkbox" name="subtitles" ${r.subtitles !== false ? "checked" : ""}> Generate subtitles for Reels when possible</label><br>
        <label><input type="checkbox" name="require_subtitles" ${r.require_subtitles ? "checked" : ""}> Reels without subtitles need approval</label><br>
        <label><input type="checkbox" name="auto_reply_templates" ${r.auto_reply_templates ? "checked" : ""}> Auto-send approved reply templates for praise/questions (not in Mode 1)</label><br>
        <label><input type="checkbox" name="publishing_paused" ${a.publishing_paused ? "checked" : ""}> Pause publishing for this account</label></div>
      <div class="row" style="grid-column:1/-1"><button class="btn primary">Save</button></div></form></div>`; }).join("") || empty("No accounts", "")}`;
  $("#bigPause").onclick = () => $("#pauseBtn").click();
  document.querySelectorAll(".autoForm").forEach((form) => form.onsubmit = async (e) => {
    e.preventDefault(); const f = new FormData(form); const id = Number(form.dataset.id); const csv = (k) => (f.get(k) || "").split(",").map((x) => x.trim()).filter(Boolean);
    let targets; try { targets = JSON.parse(f.get("monthly_targets") || "{}"); } catch { return toast("Monthly targets must be valid JSON"); }
    const a = d.accounts.find((x) => x.id === id);
    await act(async () => {
      await api(`/api/accounts/${id}`, { method: "PATCH", body: { autonomy_mode: f.get("autonomy_mode"), posts_per_week: Number(f.get("posts_per_week")), publishing_paused: !!f.get("publishing_paused") } });
      await api(`/api/accounts/${id}/brand`, { method: "PUT", body: { automation_rules: { ...(a.rules || {}), auto_formats: csv("auto_formats"), auto_pillars: csv("auto_pillars"), blocked_auto_formats: csv("blocked_auto_formats"),
        min_qc_score: Number(f.get("min_qc_score")), monthly_targets: targets, uploads_are_owned: !!f.get("uploads_are_owned"), subtitles: !!f.get("subtitles"), require_subtitles: !!f.get("require_subtitles"), auto_reply_templates: !!f.get("auto_reply_templates") } } });
    }, "Automation settings saved");
    await loadAccounts();
  });
};

// ------------------------------------------------------------------ system
VIEWS.system = async (v) => {
  const [s, log] = await Promise.all([api("/api/system"), api("/api/audit-log?limit=80")]);
  const ai = s.ai;
  v.innerHTML = `<h1>System Health & Costs</h1><p class="sub">Environment: ${esc(s.environment)} · database: ${esc(s.database.url_kind)}</p>
    <div class="kpis">
      <div class="card kpi"><div class="label">Worker</div><div class="value">${s.worker.healthy ? "✓ Running" : "! Not running"}</div><div class="delta">last heartbeat ${ago(s.worker.last_heartbeat)}</div></div>
      <div class="card kpi"><div class="label">Queued / running jobs</div><div class="value">${fmtN((s.jobs.queued || 0) + (s.jobs.running || 0))}</div></div>
      <div class="card kpi"><div class="label">Failed jobs</div><div class="value">${fmtN(s.jobs.dead || 0)}</div></div>
      <div class="card kpi"><div class="label">AI spend this month (est.)</div><div class="value">$${ai.month_to_date_usd.toFixed(2)}</div><div class="delta">budget $${ai.monthly_budget_usd.toFixed(2)}</div></div>
      <div class="card kpi"><div class="label">Media storage</div><div class="value">${(s.storage_bytes / 1e9).toFixed(2)} GB</div><div class="delta">${(s.disk_free_bytes / 1e9).toFixed(1)} GB free</div></div>
      <div class="card kpi"><div class="label">FFmpeg</div><div class="value">${s.ffmpeg ? "✓" : "✕"}</div></div></div>
    ${!s.worker.healthy ? `<div class="alert bad" style="margin:0 0 14px">The background worker is not running, so uploads are not processed and nothing publishes. Start it with <code>python run.py worker</code>.</div>` : ""}
    <div class="grid g2"><div class="card"><h2>Assets by status</h2>${Object.entries(s.assets).map(([k, n]) => `${statusBadge(k)} ${n}`).join(" ") || "<span class='muted'>none</span>"}
      <h2 style="margin-top:14px">Posts by status</h2>${Object.entries(s.posts).map(([k, n]) => `${statusBadge(k)} ${n}`).join(" ") || "<span class='muted'>none</span>"}</div>
      <div class="card"><h2>AI usage (30 days)</h2>${ai.rows.length ? `<table><tr><th>Workflow</th><th>Account</th><th class="num">Calls</th><th class="num">Cache hits</th><th class="num">Est. cost</th></tr>${ai.rows.map((r) => `<tr><td>${esc(r.workflow)}</td><td>${esc(state.accounts.find((a) => a.id === r.account_id)?.name || "—")}</td><td class="num">${r.calls}</td><td class="num">${r.cache_hits}</td><td class="num">$${r.cost_usd.toFixed(3)}</td></tr>`).join("")}</table>` : "<p class='muted'>No AI calls yet.</p>"}<p class="small muted">${esc(ai.note)}</p></div></div>
    <div class="card table-wrap" style="margin-top:14px"><h2>Failed jobs</h2>${s.failed_jobs.length ? `<table><tr><th>#</th><th>Kind</th><th>Error</th><th>Attempts</th><th>When</th><th></th></tr>${s.failed_jobs.map((j) => `<tr><td>${j.id}</td><td>${esc(j.kind)}</td><td class="small">${esc(j.last_error)}</td><td>${j.attempts}/${j.max_attempts}</td><td class="small">${fmtDate(j.updated_at)}</td><td><button class="btn" onclick="retryJob(${j.id})">Retry</button></td></tr>`).join("")}</table>` : "<p class='muted'>No failed jobs.</p>"}</div>
    <div class="card table-wrap" style="margin-top:14px"><h2>Queue</h2>${s.queue.length ? `<table><tr><th>#</th><th>Kind</th><th>Status</th><th>Runs after</th><th>Last error</th></tr>${s.queue.map((j) => `<tr><td>${j.id}</td><td>${esc(j.kind)}</td><td>${statusBadge(j.status)}</td><td class="small">${fmtDate(j.run_after)}</td><td class="small">${esc(j.last_error || "")}</td></tr>`).join("")}</table>` : "<p class='muted'>Queue is empty.</p>"}</div>
    <div class="card table-wrap" style="margin-top:14px"><h2>Audit trail</h2><table><tr><th>When</th><th>Actor</th><th>Action</th><th>Entity</th><th>Detail</th></tr>${log.map((e) => `<tr><td class="small">${fmtDate(e.ts)}</td><td class="small">${esc(e.actor)}</td><td>${esc(e.action)}</td><td class="small">${esc(e.entity)}</td><td class="small muted">${esc(JSON.stringify(e.detail).slice(0, 160))}</td></tr>`).join("")}</table></div>`;
};
window.retryJob = async (id) => { await act(() => api(`/api/jobs/${id}/retry`, { method: "POST" }), "Re-queued"); route(); };

boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div class="alert bad">${esc(e.message)}</div>`); });
