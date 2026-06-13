/* activegraph-lab notebook feed.
   Pure projection: everything rendered here comes from GET /lab/feed; the only
   writes are POST /chat (talk inside a branch) and POST /lab/decision
   (approve/reject — the inbox). No client-side state beyond the current view.
   Feed pagination: cursor on event id (/lab/entries), 'load older'. */

let FEED = null;
let VIEW = { mode: "feed", branchId: null, entityId: null };
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

/* Universal id links: any evt_N / type#N token rendered anywhere becomes a
   link into the inspector (#entity=<id>). Input must already be escaped. */
const ID_RE = /\b(evt_\d+|[a-z][a-z0-9_]*#\d+)\b/g;
function linkifyIds(escaped) {
  return escaped.replace(ID_RE,
    (m) => `<a class="id-link" href="#entity=${m}">${m}</a>`);
}
function idLink(id) {
  return `<a class="id-link" href="#entity=${escapeHtml(id)}">${escapeHtml(id)}</a>`;
}

function entryNode(e) {
  const div = document.createElement("div");
  div.className = entryClass(e.sentence);
  div.innerHTML = `<span class="when">${relTime(e.timestamp)} · ${idLink(e.event_id)}</span>` +
                  `<span class="text"></span>`;
  div.querySelector(".text").innerHTML = linkifyIds(escapeHtml(e.sentence));
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
    .map(x => `<li>${idLink(x.id)} ${linkifyIds(escapeHtml(x.text))}</li>`).join("");
  /* ADR-026: pending annotations (annotate_decision over MCP) — public,
     attributed pre-review notes. The most recent one prefills the rationale
     field when the operator resolves. */
  const notes = (d.annotations || [])
    .map(a => `<li>${idLink(a.id)} note` +
              (a.source === "operator_via_mcp" ? " (via MCP)" : "") +
              `: ${linkifyIds(escapeHtml(a.text))}</li>`).join("");
  card.innerHTML =
    `<div class="kind"><span class="chip kind-${d.kind}">${d.kind}</span> awaiting approval` +
    ` · ${idLink(d.id)}</div>` +
    `<div class="rationale">${linkifyIds(escapeHtml(d.rationale || ""))}</div>` +
    (d.subject_ref
      ? `<div class="evidence">subject: ${idLink(d.subject_ref)}` +
        (d.subject_title ? ` — ${escapeHtml(d.subject_title)}` : "") + `</div>`
      : "") +
    (ev ? `<ul class="evidence">${ev}</ul>` : "") +
    (notes ? `<ul class="evidence annotations">${notes}</ul>` : "") +
    (isOperator()
      ? `<button class="approve">Approve</button><button class="reject">Reject</button>`
      : `<span class="observer-note">awaiting the operator</span>`) +
    `<div class="resolve-form" hidden>` +
      `<textarea class="resolve-rationale" rows="2"` +
      ` placeholder="Why? Optional — recorded on the resolution event."></textarea>` +
      `<button class="confirm"></button><button class="cancel">Cancel</button>` +
    `</div>` +
    `<span class="decision-error"></span>`;
  if (isOperator()) {
    card.querySelector(".approve").onclick = () => openResolveForm(card, d, true);
    card.querySelector(".reject").onclick = () => openResolveForm(card, d, false);
  }
  return card;
}

/* ADR-026: approve/reject open an optional rationale field — skippable,
   prefilled from the most recent annotation (editable before submitting). */
function openResolveForm(card, d, approved) {
  const form = card.querySelector(".resolve-form");
  const field = card.querySelector(".resolve-rationale");
  if (form.hidden) {
    const notes = d.annotations || [];
    field.value = notes.length ? notes[notes.length - 1].text : "";
  }
  form.hidden = false;
  const confirm = card.querySelector(".confirm");
  confirm.textContent = approved ? "Confirm approve" : "Confirm reject";
  confirm.className = `confirm ${approved ? "approve" : "reject"}`;
  confirm.onclick = () => resolveDecision(card, d.id, approved, field.value.trim());
  card.querySelector(".cancel").onclick = () => { form.hidden = true; };
  field.focus();
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function resolveDecision(card, id, approved, rationale) {
  try {
    const body = { decision_id: id, approved };
    if (rationale) body.rationale = rationale;
    const r = await mutate("/lab/decision", body);
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
  /* Explicit confirm ends the composing episode — close the form so the
     next render rebuilds the inbox (composingIn no longer freezes it). */
  const form = card.querySelector(".resolve-form");
  if (form) form.hidden = true;
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
    // entity/log deep links don't need the feed — render them immediately
    if (VIEW.mode === "entity" || VIEW.mode === "log") {
      if (VIEW.mode === "entity") renderEntity(); else renderLog();
      showSections();
    }
    return;
  }

  /* C2: pending decisions visible in the tab title. */
  const pending = FEED.inbox.length;
  document.title = pending ? `(${pending}) activegraph-lab` : "activegraph-lab";

  /* C3: before mission boot. */
  if (FEED.mission) {
    $("mission-title").innerHTML =
      `${escapeHtml(FEED.mission.data.title)} · ${idLink(FEED.mission.id)}`;
  } else {
    $("mission-title").textContent = "No mission yet — the lab boots one on first run.";
  }
  const crawl = FEED.mission && FEED.mission.data.metadata.crawl;
  $("crawl").textContent = crawl ? `crawl ${crawl.fetched}/${crawl.page_cap} pages` : "";
  $("llm").textContent = `llm: ${FEED.llm.mode}${FEED.llm.model ? " · " + FEED.llm.model : ""}`;
  $("horizon").innerHTML = FEED.as_of_event
    ? `as of ${idLink(FEED.as_of_event)}` : "as of —";

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
  else if (VIEW.mode === "entity") renderEntity();
  else if (VIEW.mode === "log") renderLog();
  showSections();
}

function showSections() {
  $("feed-view").hidden = VIEW.mode !== "feed";
  $("thread-view").hidden = VIEW.mode !== "thread";
  $("seams-view").hidden = VIEW.mode !== "seams";
  $("entity-view").hidden = VIEW.mode !== "entity";
  $("log-view").hidden = VIEW.mode !== "log";
}

/* ── the inspector: one view for ANY object or event id (#entity=<id>) ────── */

let RENDERED_ENTITY = null; // refetch only when the id changes, not per poll

function fieldValueHtml(v) {
  if (v === null || v === undefined || v === "") return `<span class="dim">—</span>`;
  if (typeof v === "string") return linkifyIds(escapeHtml(v));
  if (typeof v === "number" || typeof v === "boolean") return escapeHtml(String(v));
  return `<pre class="json">${linkifyIds(escapeHtml(JSON.stringify(v, null, 2)))}</pre>`;
}

function relationListHtml(rels, arrow) {
  return rels.map(r =>
    `<div class="rel-row"><span class="chip">${escapeHtml(r.type)}</span> ${arrow} ` +
    `${idLink(r.other_id)}` +
    (r.other_title ? ` <span class="dim">${escapeHtml(r.other_title)}</span>` : "") +
    `</div>`).join("");
}

function entityObjectHtml(d) {
  const o = d.object;
  const status = o.data && o.data.status;
  const parts = [];
  parts.push(
    `<div class="entity-id"><span class="chip">${escapeHtml(o.type)}</span> ` +
    (status ? `<span class="chip ${escapeHtml(status)}">${escapeHtml(status)}</span> ` : "") +
    `<code>${escapeHtml(o.id)}</code></div>`);
  if (d.title) parts.push(`<h3 class="entity-title">${escapeHtml(d.title)}</h3>`);
  if (d.narration) {
    parts.push(`<p class="entity-narration">${linkifyIds(escapeHtml(d.narration))}</p>`);
  }
  // type-specific affordances: every chain walkable both ways
  const links = [];
  if (o.type === "branch") links.push(`<a href="#branch=${escapeHtml(o.id)}">open this branch's thread →</a>`);
  if (d.post_url) links.push(`<a href="${escapeHtml(d.post_url)}">read the published post →</a>`);
  else if (d.slug) links.push(`<a href="/lab/draft?slug=${encodeURIComponent(d.slug)}" target="_blank">read the draft markdown →</a>`);
  if (links.length) parts.push(`<div class="entity-links">${links.join(" · ")}</div>`);
  // provenance: the events that made and changed this object
  const prov = [];
  if (d.created) {
    prov.push(`created by ${idLink(d.created.event_id)}` +
      (d.created.actor ? ` <span class="dim">(${escapeHtml(d.created.actor)})</span>` : "") +
      (d.created.timestamp ? ` <span class="dim">${relTime(d.created.timestamp)}</span>` : ""));
  }
  (d.patches || []).forEach(p => {
    const keys = Object.keys(p.diff || {}).join(", ");
    prov.push(`patched by ${idLink(p.event_id)}` +
      (keys ? ` <span class="dim">(${escapeHtml(keys)})</span>` : "") +
      (p.actor ? ` <span class="dim">${escapeHtml(p.actor)}</span>` : ""));
  });
  if (prov.length) {
    parts.push(`<h4 class="register">Place in time</h4>` +
      prov.map(x => `<div class="rel-row">${x}</div>`).join(""));
  }
  // fields
  const rows = Object.entries(o.data || {})
    .map(([k, v]) => `<tr><th>${escapeHtml(k)}</th><td>${fieldValueHtml(v)}</td></tr>`);
  if (rows.length) {
    parts.push(`<h4 class="register">Fields</h4><table class="fields">${rows.join("")}</table>`);
  }
  // relations, both directions, every endpoint a link
  if ((d.relations_out || []).length) {
    parts.push(`<h4 class="register">Relations out</h4>` + relationListHtml(d.relations_out, "→"));
  }
  if ((d.relations_in || []).length) {
    parts.push(`<h4 class="register">Relations in</h4>` + relationListHtml(d.relations_in, "←"));
  }
  if (!(d.relations_out || []).length && !(d.relations_in || []).length) {
    parts.push(`<h4 class="register">Relations</h4><div class="empty">No relations recorded.</div>`);
  }
  parts.push(
    `<details class="raw"><summary>raw object JSON</summary>` +
    `<pre class="json">${linkifyIds(escapeHtml(JSON.stringify(o, null, 2)))}</pre></details>`);
  return parts.join("\n");
}

function entityEventHtml(d) {
  const e = d.event;
  const parts = [];
  parts.push(
    `<div class="entity-id"><span class="chip">event</span> ` +
    `<span class="chip">${escapeHtml(e.event_type)}</span> <code>${escapeHtml(e.id)}</code></div>`);
  parts.push(`<p class="entity-narration">${linkifyIds(escapeHtml(d.summary || e.event_type))}</p>`);
  parts.push(
    `<div class="rel-row dim">${e.actor ? `actor ${escapeHtml(e.actor)} · ` : ""}` +
    `${e.timestamp ? `${escapeHtml(e.timestamp)} · ` : ""}event ${d.index + 1} of ${d.total}</div>`);
  parts.push(
    `<div class="entity-nav">` +
    (d.prev_id ? `<a class="nav-btn" href="#entity=${d.prev_id}">← ${d.prev_id}</a>` : `<span class="nav-btn dim">← start of log</span>`) +
    (d.next_id ? `<a class="nav-btn" href="#entity=${d.next_id}">${d.next_id} →</a>` : `<span class="nav-btn dim">end of log →</span>`) +
    `</div>`);
  if ((d.refs || []).length) {
    parts.push(`<h4 class="register">Referenced entities</h4>` +
      d.refs.map(r => `<div class="rel-row">${idLink(r)}</div>`).join(""));
  }
  parts.push(
    `<details class="raw" open><summary>raw event JSON</summary>` +
    `<pre class="json">${linkifyIds(escapeHtml(JSON.stringify(e, null, 2)))}</pre></details>`);
  return parts.join("\n");
}

async function renderEntity() {
  const id = VIEW.entityId;
  if (!id || RENDERED_ENTITY === id) return;
  RENDERED_ENTITY = id;
  const box = $("entity-body");
  box.innerHTML = `<div class="empty">loading ${escapeHtml(id)}…</div>`;
  let d;
  try {
    const r = await fetch(`/lab/entity?id=${encodeURIComponent(id)}`);
    if (r.status === 404) {
      box.innerHTML = `<div class="empty">No such entity: ${escapeHtml(id)}</div>`;
      return;
    }
    if (!r.ok) throw new Error(`server returned ${r.status}`);
    d = await r.json();
  } catch (e) {
    RENDERED_ENTITY = null; // retry on next render
    box.innerHTML = `<div class="empty">Could not load ${escapeHtml(id)} — ${escapeHtml(e.message)}.</div>`;
    return;
  }
  if (id !== VIEW.entityId) return; // user already navigated on
  box.innerHTML = d.kind === "event" ? entityEventHtml(d) : entityObjectHtml(d);
}

/* ── the full event log (#log): every committed event, one row each ───────── */

let LOG_LOADED = false;
let LOG_CURSOR = null;

function logRowNode(r) {
  const div = document.createElement("div");
  div.className = "entry log-row";
  div.innerHTML =
    `<span class="when">${relTime(r.timestamp)} · ${idLink(r.event_id)}</span>` +
    `<span class="chip">${escapeHtml(r.event_type)}</span> ` +
    `<span class="text">${linkifyIds(escapeHtml(r.summary || r.event_type))}</span>`;
  return div;
}

async function fetchLogPage(before) {
  const q = before ? `?before=${encodeURIComponent(before)}&limit=100` : "?limit=100";
  const r = await fetch(`/lab/log${q}`);
  if (!r.ok) throw new Error(`server returned ${r.status}`);
  return r.json();
}

async function renderLog() {
  if (LOG_LOADED) return; // pagination state lives in the DOM; no refetch per poll
  LOG_LOADED = true;
  const box = $("log-body");
  box.innerHTML = `<div class="empty">loading the log…</div>`;
  let d;
  try {
    d = await fetchLogPage(null);
  } catch (e) {
    LOG_LOADED = false;
    box.innerHTML = `<div class="empty">Could not load the log — ${escapeHtml(e.message)}.</div>`;
    return;
  }
  box.innerHTML = `<div class="dim log-total">${d.total} events committed</div>`;
  (d.rows || []).forEach(r => box.appendChild(logRowNode(r)));
  LOG_CURSOR = d.oldest_rendered;
  if (d.more) {
    const btn = document.createElement("button");
    btn.id = "log-older";
    btn.textContent = "load older";
    btn.onclick = async () => {
      try {
        const page = await fetchLogPage(LOG_CURSOR);
        (page.rows || []).forEach(r => box.insertBefore(logRowNode(r), btn));
        LOG_CURSOR = page.oldest_rendered;
        if (!page.more) btn.remove();
      } catch (e) { /* next click retries */ }
    };
    box.appendChild(btn);
  }
}

/* 4f: the Seams view — read-only projection of /lab/seams. */
let SEAMS_LOADED = false;
async function renderSeams() {
  if (SEAMS_LOADED) return;
  SEAMS_LOADED = true;
  let data;
  try {
    data = await (await fetch("/lab/seams")).json();
  } catch (e) {
    SEAMS_LOADED = false;
    $("seams-table").textContent = "Could not load /lab/seams.";
    return;
  }
  const rows = (data.seams || []).map(s =>
    `<tr><td>${escapeHtml(s.seam_name)}</td>` +
    `<td class="src-${s.source}">${s.source}` +
    (s.active_version ? ` v${s.active_version}` : "") + `</td>` +
    `<td>${"effective_value" in s ? escapeHtml(String(s.effective_value)) : ""}</td>` +
    `<td>${(s.pending || []).length
        ? (s.pending || []).map(idLink).join(", ") + " pending" : ""}</td></tr>`
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
    `${idLink(d.id)} <span class="text"></span>`;
  div.querySelector(".text").innerHTML = linkifyIds(escapeHtml(
    (d.subject_title ? d.subject_title + " — " : "") + (d.rationale || "")));
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
  g.onclick = (ev) => {
    if (ev.target.closest("a")) return; // id links navigate to the inspector
    openThread(b.branch.id);
  };
  return g;
}

/* A rationale in progress must survive re-renders (the 3s poll, SSE pushes,
   and — on mobile — the resize/viewport churn the soft keyboard causes):
   while a resolve form is open inside a container, the container is NOT
   rebuilt, so the textarea keeps its DOM node, its typed text, and its
   focus until explicit confirm or cancel. */
function composingIn(container) {
  return !!container.querySelector(".resolve-form:not([hidden])");
}

function renderFeed() {
  renderFilters();

  // Inbox: pending decisions pinned on top — this IS the inbox, not a page.
  // Resolved decisions are the collapsed history beneath it (3a). A resolve
  // form in progress freezes the inbox block (composingIn) — the rest of
  // the feed keeps refreshing.
  const inbox = $("inbox");
  if (!composingIn(inbox)) {
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
    `<span class="chip">${d.authority}</span> ` +
    `${idLink(b.branch.id)} ` +
    linkifyIds(escapeHtml(d.intent || ""));

  const ti = $("thread-inbox");
  if (!composingIn(ti)) {
    ti.innerHTML = "";
    FEED.inbox.filter(x => x.branch_id === VIEW.branchId)
      .forEach(x => ti.appendChild(decisionCard(x)));
  }

  // ONE scroll: run events and chat messages interleaved, oldest first.
  const tl = $("timeline");
  tl.innerHTML = "";
  [...b.entries].reverse().forEach(e => tl.appendChild(entryNode(e)));
  if (!b.entries.length) {
    tl.innerHTML = `<div class="empty">Nothing committed on this branch yet.</div>`;
  }
}

/* 3b: hash routing — every view is a shareable URL: /lab#branch=<id> (post
   provenance points here), /lab#entity=<id> (the inspector), /lab#seams,
   /lab#log. The hash tracks all navigation, so the browser back button walks
   the chain the visitor just clicked through. */
function openThread(branchId) {
  if (location.hash !== `#branch=${branchId}`) {
    location.hash = `branch=${branchId}`;
  } else {
    applyHash();
  }
}

function closeThread() {
  VIEW = { mode: "feed", branchId: null };
  if (location.hash) {
    history.replaceState(null, "", location.pathname);
  }
  render();
}

function applyHash() {
  const h = location.hash || "";
  let m;
  if ((m = h.match(/^#branch=(.+)$/))) {
    VIEW = { mode: "thread", branchId: decodeURIComponent(m[1]) };
  } else if ((m = h.match(/^#entity=(.+)$/))) {
    VIEW = { mode: "entity", entityId: decodeURIComponent(m[1]) };
  } else if (h === "#seams") {
    VIEW = { mode: "seams" };
  } else if (h === "#log") {
    VIEW = { mode: "log" };
  } else {
    VIEW = { mode: "feed", branchId: null };
  }
  // leaving a lazily-loaded view resets its guard so it reloads fresh next time
  if (VIEW.mode !== "entity") RENDERED_ENTITY = null;
  if (VIEW.mode !== "log") LOG_LOADED = false;
  if (VIEW.mode !== "seams") SEAMS_LOADED = false;
  if (VIEW.mode === "seams") renderSeams();
  render();
}
window.addEventListener("hashchange", applyHash);

$("back").onclick = () => closeThread();
$("seams-back").onclick = () => closeThread();
$("log-back").onclick = () => closeThread();
$("entity-back").onclick = () => {
  // walking a provenance chain, back should retrace it; fall out to the feed
  // when the inspector was the entry point.
  if (history.length > 1) history.back();
  else closeThread();
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
