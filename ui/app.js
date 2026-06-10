/* activegraph-lab notebook feed.
   Pure projection: everything rendered here comes from GET /lab/feed; the only
   writes are POST /chat (talk inside a branch) and POST /lab/decision
   (approve/reject — the inbox). No client-side state beyond the current view.
   Feed pagination: cursor on event id (/lab/entries), 'load older'. */

let FEED = null;
let VIEW = { mode: "feed", branchId: null };
let LAST_ERROR = null;
/* 3a: the open-workshop filter row. Client-side only — the feed stays one
   projection; filters narrow what is rendered, never what exists. */
let FILTERS = { branch: "", kind: "", decision: "" };

const $ = (id) => document.getElementById(id);

/* 2c: observer mode. The token lives in localStorage, rides as a Bearer
   header on mutations, and is NEVER rendered into the DOM. */
function token() { try { return localStorage.getItem("lab_token") || ""; } catch (e) { return ""; } }
function isOperator() { return !!token(); }
function clearToken() { try { localStorage.removeItem("lab_token"); } catch (e) {} }

async function mutate(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: token() ? { "Authorization": "Bearer " + token() } : {},
    body: JSON.stringify(body),
  });
  if (r.status === 401 || r.status === 403) {
    clearToken();
    render();
    const b = await r.json().catch(() => ({}));
    throw new Error(b.error || `not authorized (${r.status})`);
  }
  return r;
}

function renderAuth() {
  const link = $("auth-link");
  const role = $("role");
  if (isOperator()) {
    role.textContent = "operator";
    link.textContent = "log out";
    link.onclick = (e) => { e.preventDefault(); clearToken(); render(); };
  } else {
    role.textContent = "observing";
    link.textContent = "operator login";
    link.onclick = (e) => {
      e.preventDefault();
      const t = window.prompt("Operator token:");
      if (t) { try { localStorage.setItem("lab_token", t.trim()); } catch (err) {} }
      render();
    };
  }
}

async function refresh() {
  try {
    const r = await fetch("/lab/feed");
    if (!r.ok) throw new Error(`feed returned ${r.status}`);
    FEED = await r.json();
    LAST_ERROR = null;
  } catch (e) {
    LAST_ERROR = "Server unreachable. Retrying every few seconds; leave this tab open.";
  }
  render();
}

/* C2: relative timestamps — "just now", "4m ago", "2h ago", then the date. */
function relTime(ts) {
  if (!ts) return "";
  const then = new Date(ts);
  if (isNaN(then)) return ts;
  const s = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return then.toISOString().slice(0, 10);
}

function entryClass(sentence) {
  if (sentence.startsWith("Lab replied")) return "entry lab";
  if (sentence.includes(" said: ")) return "entry user";
  return "entry";
}

function entryNode(e) {
  const div = document.createElement("div");
  div.className = entryClass(e.sentence);
  div.innerHTML = `<span class="when">${relTime(e.timestamp)} · ${e.event_id}</span>` +
                  `<span class="text"></span>`;
  div.querySelector(".text").textContent = e.sentence;
  if (e.post_url) {
    /* 3b: a publish event links straight to the public post. */
    const a = document.createElement("div");
    a.className = "draft-preview";
    a.innerHTML = `<a href="${escapeHtml(e.post_url)}" target="_blank">read the post →</a>`;
    div.appendChild(a);
  }
  if (e.artifact && e.artifact.slug) {
    const a = document.createElement("div");
    a.className = "draft-preview";
    /* 3b: published drafts cross-link the public post; unpublished ones the raw draft. */
    const href = e.artifact.published
      ? `/posts/${encodeURIComponent(e.artifact.slug)}`
      : `/lab/draft?slug=${encodeURIComponent(e.artifact.slug)}`;
    const label = e.artifact.published
      ? `published: /posts/${e.artifact.slug}`
      : `read draft: ${e.artifact.slug}.md`;
    a.innerHTML = `<a href="${href}" target="_blank">${escapeHtml(label)}</a>` +
                  `<div class="snippet"></div>`;
    a.querySelector(".snippet").textContent = e.artifact.preview || "";
    div.appendChild(a);
  }
  return div;
}

function entryVisible(e) {
  return !FILTERS.kind || (e.kind || "event") === FILTERS.kind;
}

function decisionCard(d) {
  const card = document.createElement("div");
  card.className = "decision-card";
  card.dataset.decisionId = d.id;
  const ev = (d.evidence || [])
    .map(x => `<li>[${x.type}] ${escapeHtml(x.text)}</li>`).join("");
  card.innerHTML =
    `<div class="kind"><span class="chip kind-${d.kind}">${d.kind}</span> awaiting approval</div>` +
    `<div class="rationale">${escapeHtml(d.rationale || "")}</div>` +
    (d.subject_title ? `<div class="evidence">subject: ${escapeHtml(d.subject_title)}</div>` : "") +
    (ev ? `<ul class="evidence">${ev}</ul>` : "") +
    (isOperator()
      ? `<button class="approve">Approve</button><button class="reject">Reject</button>`
      : `<span class="observer-note">awaiting the operator</span>`) +
    `<span class="decision-error"></span>`;
  if (isOperator()) {
    card.querySelector(".approve").onclick = () => resolveDecision(card, d.id, true);
    card.querySelector(".reject").onclick = () => resolveDecision(card, d.id, false);
  }
  return card;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function resolveDecision(card, id, approved) {
  try {
    const r = await mutate("/lab/decision", { decision_id: id, approved });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.error || `server returned ${r.status}`);
    }
  } catch (e) {
    /* C3: decision failure — plain text on the card, decision stays pending. */
    const slot = card.querySelector(".decision-error");
    if (slot) slot.textContent = ` could not apply: ${e.message}`;
    return;
  }
  refresh();
}

function render() {
  /* C3: server unreachable — one plain banner, keep last good content. */
  let banner = $("error-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "error-banner";
    document.body.prepend(banner);
  }
  banner.textContent = LAST_ERROR || "";
  banner.style.display = LAST_ERROR ? "block" : "none";
  if (!FEED) {
    if (!LAST_ERROR) {
      $("mission-title").textContent = "loading…";
    }
    return;
  }

  /* C2: pending decisions visible in the tab title. */
  const pending = FEED.inbox.length;
  document.title = pending ? `(${pending}) activegraph-lab` : "activegraph-lab";

  /* C3: before mission boot. */
  $("mission-title").textContent = FEED.mission
    ? FEED.mission.data.title
    : "No mission yet — the lab boots one on first run.";
  const crawl = FEED.mission && FEED.mission.data.metadata.crawl;
  $("crawl").textContent = crawl ? `crawl ${crawl.fetched}/${crawl.page_cap} pages` : "";
  $("llm").textContent = `llm: ${FEED.llm.mode}${FEED.llm.model ? " · " + FEED.llm.model : ""}`;
  $("horizon").textContent = `as of ${FEED.as_of_event || "—"}`;

  /* 6c: live|paused · $today/$cap, pause toggle for the operator only. */
  const st = FEED.status || {};
  $("labstatus").textContent =
    `${st.paused ? "paused" : "live"} · $${(st.llm_cost_today || 0).toFixed(2)}` +
    `/$${(st.llm_cost_cap || 0).toFixed(2)}`;
  $("labstatus").style.color = st.paused ? "var(--warn)" : "";
  const toggle = $("pause-toggle");
  toggle.hidden = !isOperator();
  toggle.textContent = st.paused ? "resume the lab" : "pause the lab";
  toggle.onclick = async () => {
    try { await mutate(st.paused ? "/lab/resume" : "/lab/pause", {}); }
    catch (e) { LAST_ERROR = `Could not ${st.paused ? "resume" : "pause"} — ${e.message}.`; }
    refresh();
  };

  renderAuth();
  $("composer").style.display = isOperator() ? "" : "none";
  if (VIEW.mode === "feed") renderFeed();
  else if (VIEW.mode === "thread") renderThread();
  $("feed-view").hidden = VIEW.mode !== "feed";
  $("thread-view").hidden = VIEW.mode !== "thread";
  $("seams-view").hidden = VIEW.mode !== "seams";
}

/* 4f: the Seams view — read-only projection of /lab/seams. */
async function renderSeams() {
  let data;
  try {
    data = await (await fetch("/lab/seams")).json();
  } catch (e) {
    $("seams-table").textContent = "Could not load /lab/seams.";
    return;
  }
  const rows = (data.seams || []).map(s =>
    `<tr><td>${escapeHtml(s.seam_name)}</td>` +
    `<td class="src-${s.source}">${s.source}` +
    (s.active_version ? ` v${s.active_version}` : "") + `</td>` +
    `<td>${"effective_value" in s ? escapeHtml(String(s.effective_value)) : ""}</td>` +
    `<td>${(s.pending || []).length ? (s.pending || []).join(", ") + " pending" : ""}</td></tr>`
  ).join("");
  $("seams-table").innerHTML =
    `<h3 class="register">Self-modification surfaces</h3>` +
    `<table class="seams"><tr><th>seam</th><th>source</th><th>value</th><th>proposals</th></tr>${rows}</table>`;
  const gc = data.graph_code || [];
  $("graph-code-table").innerHTML =
    `<h3 class="register">Graph code (${data.graph_code_enabled ? "ENABLED" : "dark — LAB_ALLOW_GRAPH_CODE unset"})</h3>` +
    (gc.length
      ? `<table class="seams"><tr><th>draft</th><th>status</th><th>state</th></tr>` +
        gc.map(d => `<tr><td>${escapeHtml(d.name)}</td><td>${d.status}</td><td>${d.state}</td></tr>`).join("") +
        `</table>`
      : `<div class="empty">No behavior or tool drafts yet.</div>`);
}

/* 3a: the filter row — branch status, event kind, decision kind. */
const BRANCH_STATUSES = ["proposed", "scoped", "active", "paused", "interpreting",
                         "decided", "archived"];
const EVENT_KINDS = ["chat", "observation", "branch", "decision", "draft", "task",
                     "evaluation", "mission", "crawl", "publish", "control"];
const DECISION_KINDS = ["publish", "promote", "self_modify"];

function filterSelect(id, label, options, current, onchange) {
  const sel = document.createElement("select");
  sel.id = id;
  sel.innerHTML = `<option value="">${label}: all</option>` +
    options.map(o => `<option value="${o}"${o === current ? " selected" : ""}>${o}</option>`).join("");
  sel.onchange = () => { onchange(sel.value); render(); };
  return sel;
}

function renderFilters() {
  const row = $("filters");
  row.innerHTML = "";
  row.appendChild(filterSelect("f-branch", "branch status", BRANCH_STATUSES,
    FILTERS.branch, v => FILTERS.branch = v));
  row.appendChild(filterSelect("f-kind", "event kind", EVENT_KINDS,
    FILTERS.kind, v => FILTERS.kind = v));
  row.appendChild(filterSelect("f-decision", "decision kind", DECISION_KINDS,
    FILTERS.decision, v => FILTERS.decision = v));
}

function resolvedRow(d) {
  const div = document.createElement("div");
  div.className = "entry resolved-decision";
  div.innerHTML =
    `<span class="chip kind-${d.kind}">${d.kind}</span> ` +
    `<span class="chip ${d.status === "approved" ? "active" : "paused"}">${d.status}</span> ` +
    `<span class="text"></span>`;
  div.querySelector(".text").textContent =
    (d.subject_title ? d.subject_title + " — " : "") + (d.rationale || "");
  return div;
}

function branchGroupNode(b) {
  const d = b.branch.data;
  const g = document.createElement("div");
  g.className = "branch-group";
  g.dataset.branchId = b.branch.id;
  g.innerHTML =
    `<div class="branch-head"><span class="title"></span>` +
    `<span class="chip ${d.status}">${d.status}</span></div>`;
  g.querySelector(".title").textContent = d.title;
  const shown = b.entries.filter(entryVisible);
  shown.slice(0, 3).forEach(e => g.appendChild(entryNode(e)));
  if (!shown.length) {
    g.insertAdjacentHTML("beforeend", `<div class="empty">No activity yet.</div>`);
  }
  g.onclick = () => { openThread(b.branch.id); };
  return g;
}

function renderFeed() {
  renderFilters();

  // Inbox: pending decisions pinned on top — this IS the inbox, not a page.
  // Resolved decisions are the collapsed history beneath it (3a).
  const inbox = $("inbox");
  inbox.innerHTML = "";
  const pending = FEED.inbox.filter(d => !FILTERS.decision || d.kind === FILTERS.decision);
  if (pending.length) {
    inbox.insertAdjacentHTML("beforeend", `<h3 class="register">Inbox — gated decisions</h3>`);
    pending.forEach(d => inbox.appendChild(decisionCard(d)));
  }
  const resolved = (FEED.resolved || [])
    .filter(d => !FILTERS.decision || d.kind === FILTERS.decision);
  if (resolved.length) {
    const det = document.createElement("details");
    det.className = "history";
    det.innerHTML = `<summary>Decision history — ${resolved.length} resolved</summary>`;
    resolved.forEach(d => det.appendChild(resolvedRow(d)));
    inbox.appendChild(det);
  }

  // Branches: open ones expanded; proposed / decided / archived collapsed (3a).
  const wrap = $("branches");
  wrap.innerHTML = `<h3 class="register">Branches</h3>`;
  let branches = FEED.branches;
  if (FILTERS.branch) {
    branches = branches.filter(b => b.branch.data.status === FILTERS.branch);
  }
  if (!branches.length) {
    wrap.insertAdjacentHTML("beforeend",
      `<div class="empty">No branches${FILTERS.branch ? ` with status ${FILTERS.branch}` : ""} yet.</div>`);
  }
  const SECTIONS = [
    ["open", ["active", "interpreting", "paused", "scoped"], true],
    ["proposed", ["proposed"], false],
    ["decided", ["decided"], false],
    ["archived", ["archived"], false],
  ];
  if (FILTERS.branch) {
    branches.forEach(b => wrap.appendChild(branchGroupNode(b)));
  } else {
    SECTIONS.forEach(([label, statuses, open]) => {
      const subset = branches.filter(b => statuses.includes(b.branch.data.status));
      if (!subset.length) return;
      if (open) {
        subset.forEach(b => wrap.appendChild(branchGroupNode(b)));
      } else {
        const det = document.createElement("details");
        det.className = "branch-section";
        det.innerHTML = `<summary>${label} — ${subset.length}</summary>`;
        subset.forEach(b => det.appendChild(branchGroupNode(b)));
        wrap.appendChild(det);
      }
    });
  }

  const ml = $("mission-log");
  ml.innerHTML = `<h3 class="register">Mission log</h3>`;
  const missionEntries = FEED.mission_entries.filter(entryVisible);
  if (!missionEntries.length) {
    ml.insertAdjacentHTML("beforeend", `<div class="empty">Nothing logged yet.</div>`);
  }
  missionEntries.slice(0, 12).forEach(e => ml.appendChild(entryNode(e)));

  // 6b: cursor pagination on event id.
  const older = document.createElement("div");
  older.id = "older";
  OLDER_ENTRIES.forEach(e => older.appendChild(entryNode(e)));
  ml.appendChild(older);
  if (FEED.oldest_rendered && (FEED.total_entries > 100 || OLDER_ENTRIES.length)) {
    const btn = document.createElement("button");
    btn.id = "load-older";
    btn.textContent = "load older";
    btn.onclick = loadOlder;
    ml.appendChild(btn);
  }
}

let OLDER_ENTRIES = [];
let OLDER_CURSOR = null;

async function loadOlder() {
  const before = OLDER_CURSOR || FEED.oldest_rendered;
  if (!before) return;
  try {
    const r = await fetch(`/lab/entries?before=${encodeURIComponent(before)}&limit=100`);
    const data = await r.json();
    OLDER_ENTRIES = OLDER_ENTRIES.concat(data.entries || []);
    OLDER_CURSOR = data.oldest_rendered;
    render();
  } catch (e) { /* next click retries */ }
}

function renderThread() {
  const b = FEED.branches.find(x => x.branch.id === VIEW.branchId);
  if (!b) { closeThread(); return; }
  const d = b.branch.data;
  $("thread-title").textContent = d.title;
  $("thread-sub").innerHTML =
    `<span class="chip ${d.status}">${d.status}</span> ` +
    `<span class="chip">${d.authority}</span> ${escapeHtml(d.intent || "")}`;

  const ti = $("thread-inbox");
  ti.innerHTML = "";
  FEED.inbox.filter(x => x.branch_id === VIEW.branchId)
    .forEach(x => ti.appendChild(decisionCard(x)));

  // ONE scroll: run events and chat messages interleaved, oldest first.
  const tl = $("timeline");
  tl.innerHTML = "";
  [...b.entries].reverse().forEach(e => tl.appendChild(entryNode(e)));
  if (!b.entries.length) {
    tl.innerHTML = `<div class="empty">Nothing committed on this branch yet.</div>`;
  }
}

/* 3b: hash routing — /lab#branch=<id> deep-links a thread (post provenance
   links land here); the hash tracks navigation so links are shareable. */
function openThread(branchId) {
  VIEW = { mode: "thread", branchId };
  if (location.hash !== `#branch=${branchId}`) {
    location.hash = `branch=${branchId}`;
  }
  render();
}

function closeThread() {
  VIEW = { mode: "feed", branchId: null };
  if (location.hash) {
    history.replaceState(null, "", location.pathname);
  }
  render();
}

function applyHash() {
  const m = (location.hash || "").match(/^#branch=(.+)$/);
  if (m) {
    VIEW = { mode: "thread", branchId: decodeURIComponent(m[1]) };
  } else if (VIEW.mode === "thread") {
    VIEW = { mode: "feed", branchId: null };
  }
  render();
}
window.addEventListener("hashchange", applyHash);

$("back").onclick = () => closeThread();
$("seams-back").onclick = () => { VIEW = { mode: "feed", branchId: null }; render(); };
$("seams-link").onclick = (e) => {
  e.preventDefault();
  VIEW = { mode: "seams", branchId: null };
  render();
  renderSeams();
};

$("composer").onsubmit = async (ev) => {
  ev.preventDefault();
  const input = $("message");
  const content = input.value.trim();
  if (!content || !VIEW.branchId) return;
  input.value = "";
  try {
    await mutate("/chat", { branch_id: VIEW.branchId, content });
  } catch (e) {
    LAST_ERROR = `Message not delivered — ${e.message}.`;
  }
  refresh();
};

/* 6a: SSE push with automatic fallback to polling. The stream is a refresh
   trigger; the data always reloads through /lab/feed (one projection). */
let POLL = null;
let SSE = null;

function startPolling() {
  if (!POLL) POLL = setInterval(refresh, 3000);
}
function stopPolling() {
  if (POLL) { clearInterval(POLL); POLL = null; }
}
function startStream() {
  if (!window.EventSource) { startPolling(); return; }
  try {
    SSE = new EventSource("/lab/stream");
    SSE.onopen = () => stopPolling();
    SSE.onmessage = () => refresh();
    SSE.onerror = () => {
      try { SSE.close(); } catch (e) {}
      SSE = null;
      startPolling();
      setTimeout(startStream, 30000); // retry SSE later
    };
  } catch (e) {
    startPolling();
  }
}

applyHash();      // /lab#branch=<id> deep-links straight into a thread
refresh();
startPolling();   // until the stream confirms open
startStream();
