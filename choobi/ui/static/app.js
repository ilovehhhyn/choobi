// choobi window. Config & inspection only. Three tabs (instructions, style, changelog);
// commands live behind the book icon; the terminal icon exposes runtime readiness.
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
history.replaceState(null, "", location.pathname);

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Choobi-Token": TOKEN }, opts.headers || {});
  const response = await fetch(path, opts);
  const body = await response.json();
  if (!response.ok || body.error) throw new Error(body.error || `request failed: ${response.status}`);
  return body;
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

const $ = (id) => document.getElementById(id);
const baseName = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p;
const when = (ts) => (ts || "").slice(0, 16).replace("T", " ");
const FACE_COUNT = 18;
const sessionFace = `/static/line-art/face-${1 + Math.floor(Math.random() * FACE_COUNT)}.png`;
document.querySelectorAll(".choobi-face").forEach((face) => { face.src = sessionFace; });

function showScreen(name) {
  for (const s of document.querySelectorAll(".screen")) s.classList.add("hidden");
  $("screen-" + name).classList.remove("hidden");
}
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  const message = event.reason?.message || "request failed";
  $("ob-error").textContent = message;
});
function wiggle() {
  document.querySelectorAll(".blob").forEach((b) => {
    b.classList.remove("wiggle"); void b.offsetWidth; b.classList.add("wiggle");
  });
}
let activePanel = "instructions";
function showPanel(name, remember) {
  document.querySelectorAll("#screen-home .panel").forEach((p) => p.classList.add("hidden"));
  $("panel-" + name).classList.remove("hidden");
  document.querySelectorAll("nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.panel === name));
  if (remember !== false) activePanel = name;
}
function showSub(panelId, subId) {
  $(panelId).querySelectorAll(".subview").forEach((v) => v.classList.add("hidden"));
  $(subId).classList.remove("hidden");
}
function makeClickable(node, action) {
  node.tabIndex = 0;
  node.setAttribute("role", "button");
  node.onclick = action;
  node.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      action();
    }
  };
}
// repos that ran choobi init, ordered by when init was run
function initedReposSorted(repos) {
  return repos.filter((r) => r.initialized).sort((a, b) => (a.first_seen || "").localeCompare(b.first_seen || ""));
}

// ---------- INSTRUCTIONS ----------
let instrRepo = null;
async function loadInstrRepos() {
  showSub("panel-instructions", "instr-repos");
  const { repos } = await api("/api/repos");
  const list = initedReposSorted(repos);
  const ul = $("instr-repo-list");
  ul.innerHTML = "";
  if (!list.length) { ul.innerHTML = '<li class="empty">// no repos yet — run `choobi init` in a repo</li>'; return; }
  for (const r of list) {
    const li = document.createElement("li");
    li.className = "clickable";
    li.textContent = baseName(r.path);
    makeClickable(li, () => openInstrRepo(r));
    ul.appendChild(li);
  }
}
function openInstrRepo(r) {
  instrRepo = r;
  $("instr-repo-path").textContent = r.path;
  $("instr-sop").classList.add("hidden");
  $("instr-kb").classList.add("hidden");
  showSub("panel-instructions", "instr-detail");
}
async function openSop() {
  $("instr-kb").classList.add("hidden");
  $("instr-sop").classList.remove("hidden");
  const r = await api("/api/repo/sop?repo=" + encodeURIComponent(instrRepo.repo_id));
  $("sop-editor").value = r.content;
  $("sop-state").textContent = r.is_default
    ? "showing the default SOP — edit and save to make it this repo's"
    : "editing this repo's saved SOP";
  $("sop-result").textContent = "";
}
async function saveSop() {
  await post("/api/repo/sop/save", { repo: instrRepo.repo_id, content: $("sop-editor").value });
  $("sop-state").textContent = "editing this repo's saved SOP";
  $("sop-result").textContent = "saved. choobi will follow this for " + baseName(instrRepo.path);
  wiggle();
}
async function resetSop() {
  const r = await post("/api/repo/sop/reset", { repo: instrRepo.repo_id });
  $("sop-editor").value = r.content;
  $("sop-state").textContent = "showing the default SOP — edit and save to make it this repo's";
  $("sop-result").textContent = "returned to the default.";
  wiggle();
}
async function openKb() {
  $("instr-sop").classList.add("hidden");
  $("instr-kb").classList.remove("hidden");
  const r = await api("/api/repo/knowledge?repo=" + encodeURIComponent(instrRepo.repo_id));
  $("kb-editor").value = r.content;
  $("kb-result").textContent = "";
}
async function regenKb() {
  const r = await post("/api/repo/knowledge/refresh", { repo: instrRepo.repo_id });
  $("kb-editor").value = r.content;
  $("kb-result").textContent = "regenerated from the repo.";
  wiggle();
}

// ---------- CHANGELOG ----------
let clRepo = null;
let clFingerprint = "";
let clPoll = null;

function stopClPolling() {
  if (clPoll) clearInterval(clPoll);
  clPoll = null;
}

function startClPolling() {
  stopClPolling();
  clPoll = setInterval(() => {
    if (activePanel === "changelog" && clRepo) refreshClLogs().catch(() => {});
  }, 2000);
}

async function loadClRepos() {
  stopClPolling();
  clRepo = null;
  clFingerprint = "";
  showSub("panel-changelog", "cl-repos");
  const { repos } = await api("/api/repos");
  const list = initedReposSorted(repos);
  const ul = $("cl-repo-list");
  ul.innerHTML = "";
  if (!list.length) { ul.innerHTML = '<li class="empty">// no repos yet — run `choobi init` in a repo</li>'; return; }
  for (const r of list) {
    const li = document.createElement("li");
    li.className = "clickable";
    li.textContent = baseName(r.path);
    makeClickable(li, () => openClRepo(r));
    ul.appendChild(li);
  }
}
async function openClRepo(r) {
  clRepo = r;
  $("cl-repo-path").textContent = r.path;
  await refreshClLogs(true);
  startClPolling();
}
async function refreshClLogs(show) {
  if (!clRepo) return;
  const repoId = clRepo.repo_id;
  const { records } = await api("/api/repo/changelog?repo=" + encodeURIComponent(repoId));
  if (!clRepo || clRepo.repo_id !== repoId) return;
  const fingerprint = records.map((rec) =>
    `${rec.id}:${rec.status}:${rec.ts}:${rec.summary}:${rec.reason}`).join("|");
  if (fingerprint === clFingerprint && !show) return;
  clFingerprint = fingerprint;
  const ul = $("cl-log-list");
  ul.innerHTML = "";
  if (!records.length) { ul.innerHTML = '<li class="empty">// empty</li>'; }
  for (const rec of records) {
    const li = document.createElement("li");
    li.className = "clickable " + rec.status;
    const title = rec.summary || (rec.status === "no_op" ? "stayed silent" : rec.reason || rec.status);
    const titleNode = document.createElement("span");
    titleNode.className = "log-title";
    titleNode.textContent = title;
    const timeNode = document.createElement("span");
    timeNode.className = "opt";
    timeNode.textContent = when(rec.ts);
    li.append(titleNode, document.createElement("br"), timeNode);
    makeClickable(li, () => openLog(rec.id));
    ul.appendChild(li);
  }
  if (show) showSub("panel-changelog", "cl-logs");
}
function renderRecord(r) {
  if (!r) return "no such entry.";
  let out = `#${r.id}  ${r.status}   ${when(r.ts)}\n`;
  out += `trigger: ${r.trigger}   duration: ${r.duration_ms}ms\n`;
  if (r.source_commit) {
    out += `source: ${r.source_commit.slice(0, 7)}`;
    if (r.docs_commit) out += `  ->  docs: ${r.docs_commit.slice(0, 7)}`;
    out += "\n";
  }
  const changed = JSON.parse(r.docs_changed || "[]");
  if (changed.length) out += `docs changed: ${changed.join(", ")}\n`;
  if (r.summary) out += `summary: ${r.summary}\n`;
  if (r.reason) out += `reason: ${r.reason}\n`;
  if (r.patch) out += `\n--- patch ---\n${r.patch}`;
  return out;
}
async function openLog(id) {
  const { record } = await api("/api/record?id=" + id);
  $("cl-detail-text").textContent = renderRecord(record);
  showSub("panel-changelog", "cl-detail");
}

// ---------- STYLE ----------
async function loadStyle() {
  const r = await api("/api/style");
  $("style-editor").value = r.content;
  $("style-result").textContent = "";
}
async function saveStyle() {
  await post("/api/style/save", { content: $("style-editor").value });
  $("style-result").textContent = "saved.";
  wiggle();
}
async function resetStyle() {
  const r = await post("/api/style/reset", {});
  $("style-editor").value = r.content;
  $("style-result").textContent = "style.md returned to the default.";
  wiggle();
}

// ---------- COMMANDS (book icon) ----------
async function loadCommands() {
  const cmds = await api("/api/commands");
  const ul = $("commands-list");
  ul.innerHTML = "";
  for (const c of cmds) {
    const li = document.createElement("li");
    const command = document.createElement("span");
    command.className = "cmd";
    command.textContent = c.command;
    const summary = document.createElement("span");
    summary.className = "desc";
    summary.textContent = c.summary;
    li.append(command, document.createElement("br"), summary);
    ul.appendChild(li);
  }
}

// ---------- RUNTIME (terminal icon tooltip + selector) ----------
function setRuntimeTooltip(cfg) {
  const label = `runtime: ${cfg.agent} · ${cfg.runtime_state}`;
  $("icon-runtime").title = label;
  $("icon-runtime").setAttribute("aria-label", label);
}

function renderRuntimePanel(cfg) {
  setRuntimeTooltip(cfg);
  $("runtime-current").textContent = `current runtime: ${cfg.agent} · ${cfg.runtime_state}`;
  for (const button of document.querySelectorAll("[data-runtime-choice]")) {
    const runtime = button.dataset.runtimeChoice;
    const active = runtime === cfg.agent;
    button.classList.toggle("active", active);
    button.textContent = active
      ? (cfg.runtime_state === "ready" ? `${runtime} (current)` : `sign in with ${runtime}`)
      : `use ${runtime}`;
    button.disabled = active && cfg.runtime_state === "ready";
  }
}

async function loadRuntimePanel() {
  const cfg = await api("/api/config");
  renderRuntimePanel(cfg);
  $("runtime-result").textContent = "";
}

async function switchRuntime(runtime) {
  const buttons = [...document.querySelectorAll("[data-runtime-choice]")];
  buttons.forEach((button) => { button.disabled = true; });
  $("runtime-result").textContent = `opening ${runtime} sign in in your browser if needed…`;
  try {
    const result = await post("/api/runtime/select", { agent: runtime });
    renderRuntimePanel(result);
    $("runtime-result").textContent = result.notes.join("\n");
    if (result.ok) wiggle();
  } catch (error) {
    $("runtime-result").textContent = error.message || "runtime switch failed";
    buttons.forEach((button) => { button.disabled = false; });
  }
}

// ---------- wiring ----------
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    const p = b.dataset.panel;
    if (p !== "changelog") stopClPolling();
    showPanel(p);
    if (p === "instructions") loadInstrRepos();
    if (p === "style") loadStyle();
    if (p === "changelog") loadClRepos();
  };
});
document.querySelectorAll("[data-back]").forEach((b) => {
  b.onclick = () => {
    if (b.dataset.back === "cl-repos") {
      stopClPolling();
      clRepo = null;
      clFingerprint = "";
    }
    showSub(b.closest(".panel").id, b.dataset.back);
  };
});
$("icon-commands").onclick = () => { showPanel("commands", false); loadCommands(); };
$("commands-back").onclick = () => showPanel(activePanel, false);
$("icon-runtime").onclick = () => {
  showPanel("runtime", false);
  loadRuntimePanel().catch((error) => {
    $("runtime-result").textContent = error.message || "could not load runtime settings";
  });
};
$("runtime-back").onclick = () => showPanel(activePanel, false);
document.querySelectorAll("[data-runtime-choice]").forEach((button) => {
  button.onclick = () => switchRuntime(button.dataset.runtimeChoice);
});
$("instr-sop-btn").onclick = openSop;
$("instr-kb-btn").onclick = openKb;
$("sop-save").onclick = saveSop;
$("sop-reset").onclick = resetSop;
$("kb-regen").onclick = regenKb;
$("style-save").onclick = saveStyle;
$("style-reset").onclick = resetStyle;

function syncOnboardRuntime() {
  $("ob-save").textContent = `sign in with ${$("ob-runtime").value}`;
}

$("ob-runtime").onchange = syncOnboardRuntime;

$("ob-save").onclick = async () => {
  const name = $("ob-name").value.trim();
  if (!name) { $("ob-error").textContent = "name required"; return; }
  const agent = $("ob-runtime").value;
  const button = $("ob-save");
  button.disabled = true;
  $("ob-error").textContent = `opening ${agent} sign in in your browser…`;
  try {
    const result = await post("/api/onboard", { name, agent });
    if (!result.ok) {
      $("ob-error").textContent = result.notes.join("\n");
      return;
    }
    wiggle();
    enterHome(await api("/api/config"));
  } finally {
    button.disabled = false;
  }
};

function enterHome(cfg) {
  setRuntimeTooltip(cfg);
  showScreen("home");
  showPanel("instructions");
  loadInstrRepos();
}

async function init() {
  const cfg = await api("/api/config");
  if (cfg.name) $("ob-name").value = cfg.name;
  if ([...$("ob-runtime").options].some((option) => option.value === cfg.agent)) {
    $("ob-runtime").value = cfg.agent;
  }
  syncOnboardRuntime();
  if (!cfg.onboarded) { showScreen("onboard"); return; }
  enterHome(cfg);
}

init();
