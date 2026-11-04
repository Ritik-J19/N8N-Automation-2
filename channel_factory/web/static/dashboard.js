"use strict";

const state = {
  csrf: "", view: "overview", loaded: new Set(), summary: null, profiles: [], jobs: [],
  jobsTotal: 0, pageSize: 50, offset: 0, sortBy: "updated_at", descending: true,
  currentJob: null, drawerTab: "overview", commandIndex: 0, refreshBusy: false, timer: null,
};
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}
function icon(name) { return `<svg class="icon" aria-hidden="true"><use href="/static/icons.svg#${name}"></use></svg>`; }
function label(value) { return String(value ?? "unknown").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
function dateTime(value) { if (!value) return "—"; const parsed = new Date(value); return Number.isNaN(parsed.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parsed); }
function timeAgo(value) {
  if (!value) return "now";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).valueOf()) / 1000));
  const units = [[86400, "d"], [3600, "h"], [60, "m"]];
  for (const [size, suffix] of units) if (seconds >= size) return `${Math.floor(seconds / size)}${suffix} ago`;
  return "just now";
}
function fileSize(bytes) { const value = Number(bytes); if (!Number.isFinite(value)) return "—"; if (value < 1024) return `${value} B`; if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1048576).toFixed(1)} MB`; }
function statusBadge(value) {
  const safe = String(value).replace(/[^a-z_]/g, "");
  const mark = ["exported", "approved", "verified", "enabled"].includes(safe) ? "check" : ["rejected", "invalid"].includes(safe) ? "alert" : "clock";
  return `<span class="status-badge ${safe}">${icon(mark)}${escapeHtml(label(value))}</span>`;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  if (options.method && !["GET", "HEAD"].includes(options.method)) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const data = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/session") showLogin();
    throw new Error(data.detail || `Request failed (${response.status})`);
  }
  return data;
}
function toast(message, error = false) { const item = document.createElement("div"); item.className = `toast${error ? " error" : ""}`; item.textContent = message; $("#toast-region").append(item); setTimeout(() => item.remove(), 4500); }
function setConnection(online) { const target = $("#connection-state"); target.classList.toggle("offline", !online); target.innerHTML = `<span class="status-pulse"></span><span>${online ? "Live" : "Offline"}</span>`; }
function showLogin() { $("#login").hidden = false; $("#app").hidden = true; state.csrf = ""; clearInterval(state.timer); $("#access-token").focus(); }
function showApp() { $("#login").hidden = true; $("#app").hidden = false; configureAutoRefresh(); switchView("overview"); }

async function bootstrap() {
  document.documentElement.dataset.theme = localStorage.getItem("cf-theme") || "dark";
  $("#refresh-interval").value = localStorage.getItem("cf-refresh") || "0";
  const filters = JSON.parse(localStorage.getItem("cf-pipeline-filters") || "{}");
  $("#job-search").value = filters.q || ""; $("#status-filter").value = filters.status || ""; $("#channel-filter").value = filters.channel || "";
  try { const session = await api("/api/session"); if (!session.authenticated) { showLogin(); return; } state.csrf = session.csrf_token; showApp(); }
  catch (_) { showLogin(); }
}

async function switchView(view) {
  state.view = view;
  $$(".view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
  $$(".nav-item[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  const titles = { overview: "Overview", pipeline: "Pipeline", channels: "Channels", reliability: "Reliability", system: "System" };
  $("#page-title").textContent = titles[view]; $("#page-kicker").textContent = view === "overview" ? "MISSION CONTROL" : "CHANNEL FACTORY";
  try { await loadView(view); setConnection(true); } catch (error) { setConnection(false); toast(error.message, true); }
}
async function loadView(view, force = false) {
  if (!force && state.loaded.has(view)) return;
  const loaders = { overview: loadOverview, pipeline: loadPipeline, channels: loadChannels, reliability: loadReliability, system: loadSystem };
  await loaders[view](); state.loaded.add(view);
}

async function loadOverview() {
  const [summary, profiles] = await Promise.all([api("/api/summary"), api("/api/profiles")]);
  state.summary = summary; state.profiles = profiles;
  $("#schema-label").textContent = `Schema v${summary.schema_version} · secured`;
  $("#pipeline-nav-count").textContent = summary.total_jobs;
  $("#incident-nav-count").textContent = summary.active_failures || "";
  const attention = summary.attention.length;
  $("#health-strip").className = `health-strip${attention ? " warning" : ""}`;
  $("#health-strip").innerHTML = `${icon(attention ? "alert" : "check")}<div><strong>${attention ? `${attention} item${attention === 1 ? "" : "s"} need attention` : "All monitored systems are healthy"}</strong><p>${attention ? "Review the queue below before advancing production." : "No unresolved incidents, stale exports, or review decisions are waiting."}</p></div>${statusBadge(attention ? "attention required" : "operational")}`;
  const metrics = [
    ["Awaiting review", summary.awaiting_review, "Human decision required", "attention", "clock"],
    ["Active incidents", summary.active_failures, summary.active_failures ? "Recovery may be needed" : "No unresolved failures", summary.active_failures ? "incident" : "positive", "alert"],
    ["Ready to export", summary.ready_to_export, "Rendered and approved", "", "pipeline"],
    ["Exports this week", summary.exports_this_week, "Across governed channels", "positive", "check"],
  ];
  $("#metric-grid").innerHTML = metrics.map(([name, value, foot, tone, glyph]) => `<article class="metric ${tone}"><div class="metric-head"><span>${escapeHtml(name)}</span>${icon(glyph)}</div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-foot">${escapeHtml(foot)}</div></article>`).join("");
  const stageLabels = { draft: "Draft", review: "Review", approved: "Approved", narrated: "Narrated", rendered: "Rendered", exported: "Exported" };
  $("#stage-rail").innerHTML = Object.entries(stageLabels).map(([key, name], index) => `<div class="stage-card${summary.stages[key] ? " has-work" : ""}"><div class="stage-name"><span>${index + 1}</span>${name}</div><div class="stage-count">${summary.stages[key]}</div></div>`).join("");
  renderAttention(summary.attention); renderActivity(summary.activity); renderCapacity(profiles); renderBackupHealth(summary.backup_health);
}
function renderAttention(items) {
  $("#attention-count").textContent = items.length;
  if (!items.length) { $("#attention-list").innerHTML = `<div class="empty-state"><div><div>${icon("check")}</div><strong>Nothing needs intervention</strong><p>The pipeline is clear and governance checks are satisfied.</p></div></div>`; return; }
  $("#attention-list").innerHTML = items.map((item) => `<button class="attention-item ${escapeHtml(item.severity)}" ${item.video_id ? `data-job-id="${escapeHtml(item.video_id)}"` : "data-view-target=\"reliability\""}><span class="attention-icon">${icon(item.severity === "error" ? "alert" : "clock")}</span><span><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)}</p></span><time>${escapeHtml(timeAgo(item.created_at))}</time></button>`).join("");
}
function renderActivity(items) {
  $("#activity-list").innerHTML = items.length ? items.map((item) => {
    const workflow = item.kind === "workflow";
    const title = workflow ? `${label(item.to_status)} · ${item.title}` : label(item.event_type);
    const detail = workflow ? (item.detail || item.channel) : item.detail;
    return `<button class="activity-item" ${workflow ? `data-job-id="${escapeHtml(item.video_id)}"` : ""}><span class="activity-icon">${icon(workflow ? "pipeline" : "system")}</span><span><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></span><time>${escapeHtml(timeAgo(item.created_at))}</time></button>`;
  }).join("") : `<div class="empty-state"><p>No activity recorded yet.</p></div>`;
}
function renderCapacity(profiles) { $("#capacity-list").innerHTML = profiles.map((profile) => `<div class="capacity-row"><div class="capacity-label"><strong>${escapeHtml(profile.name)}</strong><span>${profile.exports_this_week}/${profile.posting_cap_per_week} exports</span></div><progress value="${profile.exports_this_week}" max="${profile.posting_cap_per_week}">${profile.exports_this_week}/${profile.posting_cap_per_week}</progress></div>`).join(""); }
function renderBackupHealth(backup) {
  $("#backup-health").innerHTML = backup ? `<div class="backup-summary"><span class="backup-ring">${icon("check")}</span><div><strong>Verified recovery point</strong><p>${escapeHtml(timeAgo(backup.created_at))} · ${escapeHtml(backup.name)}</p></div></div><div class="backup-meta"><div><span>Integrity</span><strong>${escapeHtml(backup.integrity)}</strong></div><div><span>Schema</span><strong>v${escapeHtml(backup.schema_version)}</strong></div><div><span>Size</span><strong>${fileSize(backup.size)}</strong></div><div><span>Retention</span><strong>${state.summary.backup_count}/${state.summary.backup_limit}</strong></div></div>` : `<div class="empty-state"><div>${icon("backup")}<strong>No backup yet</strong><p>Create a verified recovery point.</p></div></div>`;
}

async function loadPipeline() {
  if (!state.profiles.length) state.profiles = await api("/api/profiles");
  const currentChannel = $("#channel-filter").value; const currentStatus = $("#status-filter").value;
  $("#channel-filter").innerHTML = `<option value="">All channels</option>${state.profiles.map((profile) => `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.name)}</option>`).join("")}`; $("#channel-filter").value = currentChannel;
  const statuses = ["drafted", "safety_checked", "awaiting_review", "approved", "narrated", "rendered", "exporting", "exported", "rejected"];
  $("#status-filter").innerHTML = `<option value="">All stages</option>${statuses.map((status) => `<option value="${status}">${label(status)}</option>`).join("")}`; $("#status-filter").value = currentStatus;
  await loadJobs();
}
async function loadJobs() {
  const params = new URLSearchParams({ limit: state.pageSize, offset: state.offset, sort_by: state.sortBy, descending: state.descending });
  const q = $("#job-search").value.trim(), status = $("#status-filter").value, channel = $("#channel-filter").value;
  if (q) params.set("q", q); if (status) params.set("job_status", status); if (channel) params.set("channel", channel);
  localStorage.setItem("cf-pipeline-filters", JSON.stringify({ q, status, channel }));
  const page = await api(`/api/jobs-page?${params}`); state.jobs = page.items; state.jobsTotal = page.total;
  $("#job-count").textContent = `${page.total} job${page.total === 1 ? "" : "s"}`;
  $("#jobs-body").innerHTML = state.jobs.length ? state.jobs.map((job) => `<tr class="clickable" data-job-id="${escapeHtml(job.id)}" tabindex="0"><td><div class="job-cell"><strong>${escapeHtml(job.title)}</strong><small>${escapeHtml(job.id.slice(0, 13))}</small></div></td><td>${escapeHtml(job.channel)}</td><td>${statusBadge(job.status)}</td><td>${job.has_incident ? statusBadge("incident") : statusBadge("healthy")}</td><td><span title="${escapeHtml(dateTime(job.updated_at))}">${escapeHtml(timeAgo(job.updated_at))}</span></td><td><button class="row-action" aria-label="Inspect ${escapeHtml(job.title)}">${icon("chevron")}</button></td></tr>`).join("") : `<tr><td colspan="6"><div class="empty-state"><div>${icon("search")}<strong>No matching jobs</strong><p>Adjust the search or filters.</p></div></div></td></tr>`;
  const start = page.total ? state.offset + 1 : 0, end = Math.min(state.offset + state.pageSize, page.total);
  $("#page-label").textContent = `${start}–${end} of ${page.total}`; $("#previous-page").disabled = state.offset === 0; $("#next-page").disabled = state.offset + state.pageSize >= page.total;
}

async function loadChannels() { state.profiles = await api("/api/profiles"); $("#profile-grid").innerHTML = state.profiles.map((profile) => `<article class="profile-card"><header><div><h2>${escapeHtml(profile.name)}</h2><p>${escapeHtml(profile.id)}</p></div>${statusBadge(profile.enabled ? "enabled" : "disabled")}</header><div class="profile-body"><div class="quota-block"><strong>${profile.remaining_capacity}</strong><span>exports remaining this week</span><progress value="${profile.exports_this_week}" max="${profile.posting_cap_per_week}">${profile.exports_this_week}/${profile.posting_cap_per_week}</progress></div><dl><dt>Audience</dt><dd>${escapeHtml(profile.audience)}</dd><dt>Content type</dt><dd>${escapeHtml(label(profile.content_type))}</dd><dt>Narration</dt><dd>${escapeHtml(label(profile.narration_mode))}</dd><dt>Formats</dt><dd>${escapeHtml(profile.output_formats.join(", "))}</dd><dt>Made for kids</dt><dd>${profile.made_for_kids ? "Yes" : "No"}</dd></dl></div></article>`).join(""); }

async function loadReliability() {
  const [summary, backups] = await Promise.all([api("/api/summary"), api("/api/backups")]); state.summary = summary;
  const incidents = summary.attention.filter((item) => item.kind === "incident");
  $("#reliability-metrics").innerHTML = [["Active incidents", summary.active_failures, "Unresolved workflow failures", summary.active_failures ? "incident" : "positive"], ["Verified backups", backups.filter((item) => item.valid).length, `Retention limit ${summary.backup_limit}`, "positive"], ["Latest recovery point", summary.backup_health ? timeAgo(summary.backup_health.created_at) : "None", "Database snapshot age", summary.backup_health ? "" : "attention"]].map(([name, value, foot, tone]) => `<article class="metric ${tone}"><div class="metric-head"><span>${escapeHtml(name)}</span>${icon(name.includes("backup") || name.includes("recovery") ? "backup" : "alert")}</div><div class="metric-value">${escapeHtml(value)}</div><div class="metric-foot">${escapeHtml(foot)}</div></article>`).join("");
  $("#backup-list").innerHTML = backups.length ? backups.map((backup) => `<div class="list-item"><span class="list-icon">${icon(backup.valid ? "check" : "alert")}</span><span><strong>${escapeHtml(backup.name)}</strong><p>${escapeHtml(timeAgo(backup.created_at))} · ${fileSize(backup.size)} · Schema v${escapeHtml(backup.schema_version || "?")}</p></span>${statusBadge(backup.valid ? "verified" : "invalid")}</div>`).join("") : `<div class="empty-state"><p>No backups created yet.</p></div>`;
  $("#incident-list").innerHTML = incidents.length ? incidents.map((item) => `<button class="list-item" data-job-id="${escapeHtml(item.video_id)}"><span class="list-icon">${icon("alert")}</span><span><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.detail)} · ${escapeHtml(timeAgo(item.created_at))}</p></span>${statusBadge("open")}</button>`).join("") : `<div class="empty-state"><div>${icon("check")}<strong>No active incidents</strong><p>All recorded failures are resolved.</p></div></div>`;
}

async function loadSystem() {
  const system = await api("/api/system"); $("#workspace-name").textContent = system.workspace;
  const diskFree = fileSize(system.disk.free), diskTotal = fileSize(system.disk.total);
  const cards = [
    ["Runtime", "Python", system.python, "Schema", `v${system.schema_version}`, "system"],
    ["Media engine", "FFmpeg", system.ffmpeg.status, "Provider", system.ffmpeg_provider, "pipeline"],
    ["Gemini adapter", "SDK", system.gemini_sdk.status, "Live validation", "Deferred", "channels"],
    ["Storage", "Available", diskFree, "Capacity", diskTotal, "backup"],
    ["Workspace safety", "Mode", system.workspace_storage.mode, "Guidance", system.workspace_storage.sync_folder_detected ? "Move active DB local" : "Ready", "alert"],
  ];
  $("#system-grid").innerHTML = cards.map(([title, k1, v1, k2, v2, glyph]) => `<article class="system-card"><header><div><h2>${escapeHtml(title)}</h2><p>Operational dependency</p></div>${icon(glyph)}</header><div class="system-body"><dl><dt>${escapeHtml(k1)}</dt><dd>${escapeHtml(label(v1))}</dd><dt>${escapeHtml(k2)}</dt><dd>${escapeHtml(label(v2))}</dd></dl></div></article>`).join("");
  const controls = [["Trusted hosts", "DNS rebinding requests rejected"], ["Login throttling", "Repeated failures are rate limited"], ["Revocable sessions", "Logout invalidates captured cookies"], ["CSRF + origin", "Mutations require two same-origin checks"]];
  $("#security-grid").innerHTML = controls.map(([name, detail]) => `<div class="security-control">${icon("check")}<strong>${escapeHtml(name)}</strong><p>${escapeHtml(detail)}</p></div>`).join("");
}

async function inspectJob(videoId) {
  const dialog = $("#job-dialog"); if (!dialog.open) dialog.showModal();
  $("#drawer-title").textContent = "Loading job…"; $("#drawer-content").innerHTML = `<div class="empty-state"><p>Retrieving audit record…</p></div>`;
  try { state.currentJob = await api(`/api/jobs/${encodeURIComponent(videoId)}`); state.drawerTab = "overview"; renderInspector(); $("#drawer-title").focus(); }
  catch (error) { $("#drawer-content").innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; }
}
function renderInspector() {
  const job = state.currentJob; if (!job) return;
  $("#drawer-title").textContent = job.title; $("#drawer-subtitle").textContent = `${job.id} · Updated ${timeAgo(job.updated_at)}`; $("#drawer-status").innerHTML = statusBadge(job.status); $("#drawer-channel").textContent = job.channel;
  $$("#drawer-tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === state.drawerTab));
  const renders = { overview: renderJobOverview, timeline: renderJobTimeline, script: renderJobScript, assets: renderJobAssets, incidents: renderJobIncidents };
  $("#drawer-content").innerHTML = renders[state.drawerTab](job);
}
function renderJobOverview(job) {
  const checks = job.readiness.checks.filter((check) => check.passed !== null).map((check) => `<div class="check-item${check.passed ? "" : " failed"}">${icon(check.passed ? "check" : "alert")}<span>${escapeHtml(check.label)}</span></div>`).join("");
  const artifacts = Object.entries(job.artifacts).flatMap(([kind, value]) => (Array.isArray(value) ? value : value ? [value] : []).map((path) => `<div class="artifact-row"><strong>${escapeHtml(label(kind))}</strong><span>${escapeHtml(path)}</span><button class="row-action copy-path" data-copy="${escapeHtml(path)}" aria-label="Copy path">${icon("copy")}</button></div>`)).join("");
  const recoverable = ["narrated", "rendered", "exporting"].includes(job.status);
  return `<section class="drawer-section"><div class="readiness-hero"><div class="score-ring">${job.readiness.score}%</div><div><h3>Production readiness</h3><p>${job.readiness.passed} of ${job.readiness.total} applicable checks are complete.</p></div></div><div class="check-list">${checks}</div></section><section class="drawer-section"><h3>Job context</h3><div class="job-meta"><div class="meta-box"><span>Channel</span><strong>${escapeHtml(job.channel)}</strong></div><div class="meta-box"><span>Topic</span><strong>${escapeHtml(job.topic_id)}</strong></div><div class="meta-box"><span>Created</span><strong>${escapeHtml(dateTime(job.created_at))}</strong></div><div class="meta-box"><span>Approved</span><strong>${escapeHtml(dateTime(job.approved_at))}</strong></div><div class="meta-box"><span>Exported</span><strong>${escapeHtml(dateTime(job.exported_at))}</strong></div><div class="meta-box"><span>Incidents</span><strong>${job.failures.filter((item) => !item.resolved_at).length}</strong></div></div></section><section class="drawer-section"><h3>Artifacts</h3><div class="artifact-list">${artifacts || "<p class='muted'>No generated artifacts yet.</p>"}</div></section><div class="drawer-actions">${recoverable ? `<button class="button primary" id="recover-job" data-job-id="${escapeHtml(job.id)}">Recover next safe stage</button>` : ""}<span class="muted">Approval remains terminal-only.</span></div>`;
}
function renderJobTimeline(job) { return `<section class="drawer-section"><h3>Complete workflow history</h3><div class="timeline">${job.events.slice().reverse().map((event) => `<div class="timeline-item"><strong>${escapeHtml(label(event.to_status))}</strong><p>${escapeHtml(event.detail || "State transition")} · ${escapeHtml(dateTime(event.created_at))}</p></div>`).join("")}</div></section>`; }
function renderJobScript(job) { const scenes = job.script.scenes.map((scene, index) => `<article class="scene-card"><header><strong>Scene ${index + 1}</strong><span>${escapeHtml(scene.duration_sec)}s · ${escapeHtml(scene.scene_id)}</span></header><p>${escapeHtml(scene.narration)}</p></article>`).join(""); return `<section class="drawer-section"><h3>Structured scenes</h3>${scenes}</section><section class="drawer-section"><h3>Validated script JSON</h3><pre class="code-block">${escapeHtml(JSON.stringify(job.script, null, 2))}</pre></section>`; }
function renderJobAssets(job) { return `<section class="drawer-section"><h3>Provenance ledger</h3>${job.assets.length ? job.assets.map((asset) => `<article class="scene-card"><header><strong>${escapeHtml(asset.asset_id)}</strong><span>${escapeHtml(asset.license)}</span></header><p>${escapeHtml(asset.source)}</p><p>${escapeHtml(asset.attribution)}</p><div class="artifact-row"><span>${escapeHtml(asset.path)}</span><button class="row-action copy-path" data-copy="${escapeHtml(asset.path)}" aria-label="Copy path">${icon("copy")}</button></div></article>`).join("") : "<p class='muted'>No registered assets.</p>"}</section>`; }
function renderJobIncidents(job) { return `<section class="drawer-section"><h3>Failure history</h3>${job.failures.length ? job.failures.map((failure) => `<article class="incident-card"><strong>${escapeHtml(label(failure.operation))} · ${escapeHtml(failure.error_type)}</strong><p>${escapeHtml(failure.message)}</p><small>${failure.resolved_at ? `Resolved ${escapeHtml(dateTime(failure.resolved_at))}` : `Open · ${escapeHtml(dateTime(failure.created_at))}`}</small></article>`).join("") : `<div class="empty-state"><div>${icon("check")}<strong>No recorded incidents</strong><p>This job has a clean operational history.</p></div></div>`}</section>`; }

function confirmAction(message, actionLabel = "Continue") { return new Promise((resolve) => { const dialog = $("#confirm-dialog"); $("#confirm-message").textContent = message; $("#confirm-accept").textContent = actionLabel; dialog.showModal(); const finish = (value) => { dialog.close(); resolve(value); }; $("#confirm-cancel").onclick = () => finish(false); $("#confirm-accept").onclick = () => finish(true); }); }
async function recoverJob(videoId) { if (!await confirmAction("This will resume the next safe production stage for an already-approved job.", "Run recovery")) return; try { const result = await api(`/api/jobs/${encodeURIComponent(videoId)}/recover`, { method: "POST" }); toast(`${label(result.operation)} completed.`); invalidateAll(); await inspectJob(videoId); } catch (error) { toast(error.message, true); } }
async function createBackup() { if (!await confirmAction("Create and integrity-check a new SQLite recovery point?", "Create backup")) return; try { const result = await api("/api/backups", { method: "POST" }); toast(`Verified backup created: ${result.name}`); invalidateAll(); await loadView(state.view, true); } catch (error) { toast(error.message, true); } }

function invalidateAll() { state.loaded.clear(); }
async function refreshCurrent(silent = false) { if (state.refreshBusy || document.hidden) return; state.refreshBusy = true; try { state.loaded.delete(state.view); await loadView(state.view); setConnection(true); if (!silent) toast("Mission Control refreshed."); } catch (error) { setConnection(false); if (!silent) toast(error.message, true); } finally { state.refreshBusy = false; } }
function configureAutoRefresh() { clearInterval(state.timer); const seconds = Number($("#refresh-interval").value); localStorage.setItem("cf-refresh", String(seconds)); if (seconds) state.timer = setInterval(() => refreshCurrent(true), seconds * 1000); }

function downloadData(format) { const fields = state.jobs.map(({ id, title, channel, topic_id, status, created_at, updated_at }) => ({ id, title, channel, topic_id, status, created_at, updated_at })); let content, type, extension; if (format === "json") { content = JSON.stringify(fields, null, 2); type = "application/json"; extension = "json"; } else { const headers = Object.keys(fields[0] || { id: "", title: "", channel: "", topic_id: "", status: "", created_at: "", updated_at: "" }); const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`; content = [headers.join(","), ...fields.map((row) => headers.map((key) => quote(row[key])).join(","))].join("\r\n"); type = "text/csv"; extension = "csv"; } const url = URL.createObjectURL(new Blob([content], { type })); const link = document.createElement("a"); link.href = url; link.download = `channel-factory-jobs.${extension}`; link.click(); URL.revokeObjectURL(url); }

function commandItems() { const base = [{ type: "Navigation", title: "Go to Overview", detail: "Operational health and attention", action: "view:overview", icon: "overview" }, { type: "Navigation", title: "Go to Pipeline", detail: "Search and inspect workflow jobs", action: "view:pipeline", icon: "pipeline" }, { type: "Navigation", title: "Go to Channels", detail: "Capacity and governance", action: "view:channels", icon: "channels" }, { type: "Navigation", title: "Go to Reliability", detail: "Backups and incidents", action: "view:reliability", icon: "reliability" }, { type: "Action", title: "Refresh current view", detail: "Retrieve the latest persisted state", action: "refresh", icon: "refresh" }, { type: "Action", title: "Create verified backup", detail: "CSRF-protected local snapshot", action: "backup", icon: "backup" }, { type: "Preference", title: "Switch theme", detail: "Toggle dark and light appearance", action: "theme", icon: "sun" }]; return [...base, ...state.jobs.map((job) => ({ type: "Jobs", title: job.title, detail: `${job.channel} · ${label(job.status)}`, action: `job:${job.id}`, icon: "pipeline" }))]; }
function openCommandPalette() { const dialog = $("#command-dialog"); if (!dialog.open) dialog.showModal(); $("#command-query").value = ""; state.commandIndex = 0; renderCommands(); $("#command-query").focus(); }
function renderCommands() { const query = $("#command-query").value.trim().toLowerCase(); const items = commandItems().filter((item) => !query || `${item.title} ${item.detail}`.toLowerCase().includes(query)).slice(0, 14); $("#command-results").innerHTML = items.length ? items.map((item, index) => `<button class="command-item${index === state.commandIndex ? " active" : ""}" data-command="${escapeHtml(item.action)}"><span class="command-icon">${icon(item.icon)}</span><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.detail)}</small></span><kbd>↵</kbd></button>`).join("") : `<div class="empty-state"><p>No matching command or job.</p></div>`; }
async function runCommand(action) { $("#command-dialog").close(); if (action.startsWith("view:")) return switchView(action.split(":")[1]); if (action.startsWith("job:")) return inspectJob(action.split(":")[1]); if (action === "refresh") return refreshCurrent(); if (action === "backup") return createBackup(); if (action === "theme") return toggleTheme(); }
function toggleTheme() { const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = theme; localStorage.setItem("cf-theme", theme); }

$("#login-form").addEventListener("submit", async (event) => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); button.disabled = true; $("#login-error").textContent = ""; try { const result = await api("/api/session", { method: "POST", body: JSON.stringify({ token: $("#access-token").value }) }); state.csrf = result.csrf_token; $("#access-token").value = ""; showApp(); } catch (error) { $("#login-error").textContent = error.message; } finally { button.disabled = false; } });
$("#nav").addEventListener("click", (event) => { const button = event.target.closest("[data-view]"); if (button) switchView(button.dataset.view); });
document.addEventListener("click", (event) => { const jobTarget = event.target.closest("[data-job-id]"); if (jobTarget && !event.target.closest("#recover-job")) inspectJob(jobTarget.dataset.jobId); const goto = event.target.closest("[data-goto]"); if (goto) switchView(goto.dataset.goto); const viewTarget = event.target.closest("[data-view-target]"); if (viewTarget) switchView(viewTarget.dataset.viewTarget); const copy = event.target.closest("[data-copy]"); if (copy) navigator.clipboard.writeText(copy.dataset.copy).then(() => toast("Path copied.")); const command = event.target.closest("[data-command]"); if (command) runCommand(command.dataset.command); });
$("#jobs-body").addEventListener("keydown", (event) => { if ((event.key === "Enter" || event.key === " ") && event.target.matches("tr[data-job-id]")) { event.preventDefault(); inspectJob(event.target.dataset.jobId); } });
let searchDelay; $("#job-search").addEventListener("input", () => { clearTimeout(searchDelay); searchDelay = setTimeout(() => { state.offset = 0; loadJobs().catch((error) => toast(error.message, true)); }, 220); });
[$("#status-filter"), $("#channel-filter")].forEach((control) => control.addEventListener("change", () => { state.offset = 0; loadJobs().catch((error) => toast(error.message, true)); }));
document.querySelector("thead").addEventListener("click", (event) => { const button = event.target.closest("[data-sort]"); if (!button) return; if (state.sortBy === button.dataset.sort) state.descending = !state.descending; else { state.sortBy = button.dataset.sort; state.descending = true; } state.offset = 0; loadJobs().catch((error) => toast(error.message, true)); });
$("#previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.pageSize); loadJobs(); }); $("#next-page").addEventListener("click", () => { state.offset += state.pageSize; loadJobs(); });
$("#drawer-tabs").addEventListener("click", (event) => { const button = event.target.closest("[data-tab]"); if (button) { state.drawerTab = button.dataset.tab; renderInspector(); } });
$("#close-drawer").addEventListener("click", () => $("#job-dialog").close()); $("#copy-job-id").addEventListener("click", () => { if (state.currentJob) navigator.clipboard.writeText(state.currentJob.id).then(() => toast("Job ID copied.")); });
$("#drawer-content").addEventListener("click", (event) => { const recover = event.target.closest("#recover-job"); if (recover) recoverJob(recover.dataset.jobId); });
[$("#create-backup"), $("#quick-backup")].forEach((button) => button.addEventListener("click", createBackup));
$("#refresh").addEventListener("click", () => refreshCurrent()); $("#refresh-interval").addEventListener("change", configureAutoRefresh); $("#theme-toggle").addEventListener("click", toggleTheme);
$("#export-csv").addEventListener("click", () => downloadData("csv")); $("#export-json").addEventListener("click", () => downloadData("json"));
$("#command-trigger").addEventListener("click", openCommandPalette); $("#command-query").addEventListener("input", () => { state.commandIndex = 0; renderCommands(); });
$("#command-query").addEventListener("keydown", (event) => { const items = $$("#command-results .command-item"); if (event.key === "ArrowDown") { event.preventDefault(); state.commandIndex = Math.min(items.length - 1, state.commandIndex + 1); renderCommands(); } else if (event.key === "ArrowUp") { event.preventDefault(); state.commandIndex = Math.max(0, state.commandIndex - 1); renderCommands(); } else if (event.key === "Enter" && items[state.commandIndex]) { event.preventDefault(); runCommand(items[state.commandIndex].dataset.command); } });
document.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openCommandPalette(); } });
$("#logout").addEventListener("click", async () => { try { await api("/api/session", { method: "DELETE" }); } finally { invalidateAll(); showLogin(); } });
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshCurrent(true); });

bootstrap();
