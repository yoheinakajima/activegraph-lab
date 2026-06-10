/* activegraph-lab notebook feed.
   Pure projection: everything rendered here comes from GET /lab/feed; the only
   writes are POST /chat (talk inside a branch) and POST /lab/decision
   (approve/reject — the inbox). No client-side state beyond the current view.
   Feed pagination: OPEN (docs/INTERFACE.md) — full projection for now. */

let FEED = null;
let VIEW = { mode: "feed", branchId: null };
let LAST_ERROR = null;

const $ = (id) => document.getElementById(id);

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
  if (e.artifact && e.artifact.slug) {
    const a = document.createElement("div");
    a.className = "draft-preview";
    a.innerHTML = `<a href="/lab/draft?slug=${encodeURIComponent(e.artifact.slug)}" ` +
                  `target="_blank">read draft: ${escapeHtml(e.artifact.slug)}.md</a>` +
                  `<div class="snippet"></div>`;
    a.querySelector(".snippet").textContent = e.artifact.preview || "";
    div.appendChild(a);
  }
  return div;
}

function decisionCard(d) {
  const card = document.createElement("div");
  card.className = "decision-card";
  card.dataset.decisionId = d.id;
  const ev = (d.evidence || [])
    .map(x => `<li>[${x.type}] ${escapeHtml(x.text)}</li>`).join("");
  card.innerHTML =
    `<div class="kind">${d.kind} — awaiting approval</div>` +
    `<div class="rationale">${escapeHtml(d.rationale || "")}</div>` +
    (d.subject_title ? `<div class="evidence">subject: ${escapeHtml(d.subject_title)}</div>` : "") +
    (ev ? `<ul class="evidence">${ev}</ul>` : "") +
    `<button class="approve">Approve</button><button class="reject">Reject</button>` +
    `<span class="decision-error"></span>`;
  card.querySelector(".approve").onclick = () => resolveDecision(card, d.id, true);
  card.querySelector(".reject").onclick = () => resolveDecision(card, d.id, false);
  return card;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function resolveDecision(card, id, approved) {
  try {
    const r = await fetch("/lab/decision", {
      method: "POST",
      body: JSON.stringify({ decision_id: id, approved }),
    });
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

  if (VIEW.mode === "feed") renderFeed(); else renderThread();
  $("feed-view").hidden = VIEW.mode !== "feed";
  $("thread-view").hidden = VIEW.mode !== "thread";
}

function renderFeed() {
  // Inbox: pending decisions pinned on top — this IS the inbox, not a page.
  const inbox = $("inbox");
  inbox.innerHTML = "";
  if (FEED.inbox.length) {
    inbox.insertAdjacentHTML("beforeend", `<h3 class="register">Inbox — gated decisions</h3>`);
    FEED.inbox.forEach(d => inbox.appendChild(decisionCard(d)));
  }

  const wrap = $("branches");
  wrap.innerHTML = `<h3 class="register">Branches</h3>`;
  if (!FEED.branches.length) {
    wrap.insertAdjacentHTML("beforeend",
      `<div class="empty">No branches yet — the seed branch appears after first boot.</div>`);
  }
  FEED.branches.forEach(b => {
    const d = b.branch.data;
    const g = document.createElement("div");
    g.className = "branch-group";
    g.dataset.branchId = b.branch.id;
    g.innerHTML =
      `<div class="branch-head"><span class="title"></span>` +
      `<span class="chip ${d.status}">${d.status}</span></div>`;
    g.querySelector(".title").textContent = d.title;
    b.entries.slice(0, 3).forEach(e => g.appendChild(entryNode(e)));
    if (!b.entries.length) {
      g.insertAdjacentHTML("beforeend", `<div class="empty">No activity yet.</div>`);
    }
    g.onclick = () => { VIEW = { mode: "thread", branchId: b.branch.id }; render(); };
    wrap.appendChild(g);
  });

  const ml = $("mission-log");
  ml.innerHTML = `<h3 class="register">Mission log</h3>`;
  if (!FEED.mission_entries.length) {
    ml.insertAdjacentHTML("beforeend", `<div class="empty">Nothing logged yet.</div>`);
  }
  FEED.mission_entries.slice(0, 12).forEach(e => ml.appendChild(entryNode(e)));
}

function renderThread() {
  const b = FEED.branches.find(x => x.branch.id === VIEW.branchId);
  if (!b) { VIEW = { mode: "feed", branchId: null }; renderFeed(); return; }
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

$("back").onclick = () => { VIEW = { mode: "feed", branchId: null }; render(); };

$("composer").onsubmit = async (ev) => {
  ev.preventDefault();
  const input = $("message");
  const content = input.value.trim();
  if (!content || !VIEW.branchId) return;
  input.value = "";
  try {
    await fetch("/chat", {
      method: "POST",
      body: JSON.stringify({ branch_id: VIEW.branchId, content }),
    });
  } catch (e) {
    LAST_ERROR = "Message not delivered — server unreachable.";
  }
  refresh();
};

refresh();
setInterval(refresh, 3000);
