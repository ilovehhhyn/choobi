// choobi window. Config & inspection only. Three tabs (instructions, style, changelog);
// commands live behind the book icon; the terminal icon shows the runtime on hover.
"use strict";

const TOKEN = new URLSearchParams(location.search).get("token") || "";
history.replaceState(null, "", location.pathname);

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ "X-Choobi-Token": TOKEN }, opts.headers || {});
  return (await fetch(path, opts)).json();
}
const post = (path, body) => api(path, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

const $ = (id) => document.getElementById(id);
const baseName = (p) => (p || "").replace(/\/+$/, "").split("/").pop() || p;
const when = (ts) => (ts || "").slice(0, 16).replace("T", " ");

function showScreen(name) {
  for (const s of document.querySelectorAll(".screen")) s.classList.add("hidden");
  $("screen-" + name).classList.remove("hidden");
}
function setStatus(s) { $("status").textContent = s; }
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
    li.onclick = () => openInstrRepo(r);
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
async function saveKb() {
  await post("/api/repo/knowledge/save", { repo: instrRepo.repo_id, content: $("kb-editor").value });
  $("kb-result").textContent = "saved.";
  wiggle();
}
async function regenKb() {
  const r = await post("/api/repo/knowledge/refresh", { repo: instrRepo.repo_id });
  $("kb-editor").value = r.content;
  $("kb-result").textContent = "regenerated from the repo (unsaved edits replaced).";
  wiggle();
}

// ---------- CHANGELOG ----------
async function loadClRepos() {
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
    li.onclick = () => openClRepo(r);
    ul.appendChild(li);
  }
}
async function openClRepo(r) {
  $("cl-repo-path").textContent = r.path;
  const { records } = await api("/api/repo/changelog?repo=" + encodeURIComponent(r.repo_id));
  const ul = $("cl-log-list");
  ul.innerHTML = "";
  if (!records.length) { ul.innerHTML = '<li class="empty">// empty</li>'; }
  for (const rec of records) {
    const li = document.createElement("li");
    li.className = "clickable " + rec.status;
    const title = rec.summary || (rec.status === "no_op" ? "stayed silent" : rec.reason || rec.status);
    li.innerHTML = `<span class="log-title">${title}</span><br><span class="opt">${when(rec.ts)}</span>`;
    li.onclick = () => openLog(rec.id);
    ul.appendChild(li);
  }
  showSub("panel-changelog", "cl-logs");
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
function styleStateLabel(isPersonal) {
  return isPersonal
    ? "editing your personal style guide (~/.choobi/style.md)"
    : "showing the built-in default — edit and save to make it your own";
}
async function loadStyle() {
  const r = await api("/api/style");
  $("style-editor").value = r.content;
  $("style-state").textContent = styleStateLabel(r.is_personal);
  $("style-result").textContent = "";
}
async function saveStyle() {
  const r = await post("/api/style/save", { content: $("style-editor").value });
  $("style-state").textContent = styleStateLabel(r.is_personal);
  $("style-result").textContent = "saved.";
  wiggle();
}
async function resetStyle() {
  const r = await post("/api/style/reset", {});
  $("style-editor").value = r.content;
  $("style-state").textContent = styleStateLabel(r.is_personal);
  $("style-result").textContent = "returned to the default.";
  wiggle();
}

// ---------- COMMANDS (book icon) ----------
async function loadCommands() {
  const cmds = await api("/api/commands");
  const ul = $("commands-list");
  ul.innerHTML = "";
  for (const c of cmds) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="cmd">${c.command}</span><br><span class="desc">${c.summary}</span>`;
    ul.appendChild(li);
  }
}

// ---------- RUNTIME (terminal icon tooltip) ----------
function setRuntimeTooltip(cfg) {
  const model = cfg.agent === "codex" ? "codex default model" : "Claude Code default model";
  $("icon-runtime").title = `runtime: ${cfg.agent} · model: ${model}`;
}

// ---------- wiring ----------
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    const p = b.dataset.panel;
    showPanel(p);
    if (p === "instructions") loadInstrRepos();
    if (p === "style") loadStyle();
    if (p === "changelog") loadClRepos();
  };
});
document.querySelectorAll("[data-back]").forEach((b) => {
  b.onclick = () => showSub(b.closest(".panel").id, b.dataset.back);
});
$("icon-commands").onclick = () => { showPanel("commands", false); loadCommands(); };
$("commands-back").onclick = () => showPanel(activePanel, false);
$("instr-sop-btn").onclick = openSop;
$("instr-kb-btn").onclick = openKb;
$("sop-save").onclick = saveSop;
$("sop-reset").onclick = resetSop;
$("kb-save").onclick = saveKb;
$("kb-regen").onclick = regenKb;
$("style-save").onclick = saveStyle;
$("style-reset").onclick = resetStyle;

$("ob-save").onclick = async () => {
  const name = $("ob-name").value.trim();
  if (!name) { $("ob-error").textContent = "name required"; return; }
  await post("/api/onboard", { name, api_key: $("ob-key").value.trim(), agent: $("ob-agent").value });
  wiggle();
  enterHome(await api("/api/config"));
};

function enterHome(cfg) {
  setRuntimeTooltip(cfg);
  showScreen("home");
  showPanel("instructions");
  loadInstrRepos();
  setStatus("ready");
}

async function init() {
  const cfg = await api("/api/config");
  if (cfg.name) $("ob-name").value = cfg.name;
  if (!cfg.onboarded) { showScreen("onboard"); return; }
  enterHome(cfg);
}

init();
