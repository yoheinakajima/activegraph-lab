/* activegraph-lab notebook feed.
   Pure projection: everything rendered here comes from GET /lab/feed; the only
   writes are POST /chat (talk inside a branch) and POST /lab/decision
   (approve/reject — the inbox). No client-side state beyond the current view.
   Feed pagination: OPEN (docs/INTERFACE.md) — full projection for now. */

let FEED = null;
let VIEW = { mode: "feed", branchId: null };

const $ = (id) => document.getElementById(id);

async function refresh() {
  try {
    const r = await fetch("/lab/feed");
    FEED = await r.json();
    render();
  } catch (e) { /* server restarting; next poll wins */ }
}

function fmtTime(ts) {
  if (!ts) return "";
  const m = ts.match(/T(\d\d:\d\d:\d\d)/);
  return m ? m[1] : ts;
}

function entryClass(sentence) {
  if (sentence.startsWith("Lab replied")) return "entry lab";
  if (sentence.includes(" said: ")) return "entry user";
  return "entry";
}

function entryNode(e) {
  const div = document.createElement("div");
  div.className = entryClass(e.sentence);
  div.innerHTML = `<span class="when">${fmtTime(e.timestamp)} ${e.event_id}</span>` +
                  `<span class="text"></span>`;
  div.querySelector(".text").textContent = e.sentence;
  return div;
}

function decisionCard(d) {
  const card = document.createElement("div");
  card.className = "decision-card";
  const ev = (d.evidence || [])
    .map(x => `<li>[${x.type}] ${escapeHtml(x.text)}</li>`).join("");
  card.innerHTML =
    `<div class="kind">${d.kind} — awaiting approval</div>` +
    `<div class="rationale">${escapeHtml(d.rationale || "")}</div>` +
    (d.subject_title ? `<div class="evidence">subject: ${escapeHtml(d.subject_title)}</div>` : "") +
    (ev ? `<ul class="evidence">${ev}</ul>` : "") +
    `<button class="approve">Approve</button><button class="reject">Reject</button>`;
  card.querySelector(".approve").onclick = () => resolveDecision(d.id, true);
  card.querySelector(".reject").onclick = () => resolveDecision(d.id, false);
  return card;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

async function resolveDecision(id, approved) {
  await fetch("/lab/decision", {
    method: "POST",
    body: JSON.stringify({ decision_id: id, approved }),
  });
  refresh();
}

function render() {
  if (!FEED) return;
  $("mission-title").textContent = FEED.mission ? FEED.mission.data.title : "(no mission)";
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
    wrap.insertAdjacentHTML("beforeend", `<div class="empty">No branches yet.</div>`);
  }
  FEED.branches.forEach(b => {
    const d = b.branch.data;
    const g = document.createElement("div");
    g.className = "branch-group";
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
  await fetch("/chat", {
    method: "POST",
    body: JSON.stringify({ branch_id: VIEW.branchId, content }),
  });
  refresh();
};

refresh();
setInterval(refresh, 3000);
