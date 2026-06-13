// Render verification for the notebook feed (C1). Invoked by check_ui.py.
// Usage: node scripts/check_ui.mjs <feed.json> <ui_dir> [jsdom_dir]
//
// Loads index.html + app.js in jsdom with fetch stubbed to serve the real
// /lab/feed JSON produced by a live lab run, then asserts the rendered DOM:
// inbox cards, branch groups, thread-view timeline interleaving, and that
// the decision buttons POST to /lab/decision.

import { readFileSync } from "fs";
import { createRequire } from "module";
import path from "path";

const [bundleFile, uiDir, jsdomDir] = process.argv.slice(2);
const require = createRequire(
  jsdomDir ? path.join(jsdomDir, "/") : import.meta.url);
const { JSDOM } = require("jsdom");

const bundle = JSON.parse(readFileSync(bundleFile, "utf8"));
const feed = bundle.feed || bundle; // bundle: {feed, entities, log, …}
const entities = bundle.entities || {};
const logPage = bundle.log || { rows: [], total: 0, more: false };
const html = readFileSync(path.join(uiDir, "index.html"), "utf8")
  .replace(/<script src="\/app\.js"><\/script>/, "");
const appJs = readFileSync(path.join(uiDir, "app.js"), "utf8");

const failures = [];
const check = (cond, msg) => {
  console.log(`  [${cond ? "ok" : "FAIL"}] ${msg}`);
  if (!cond) failures.push(msg);
};

const posts = [];
const dom = new JSDOM(html, { url: "http://localhost/", runScripts: "dangerously" });
const { window } = dom;
window.fetch = async (url, opts = {}) => {
  if ((opts.method || "GET") === "POST") {
    posts.push({ url, body: JSON.parse(opts.body || "{}"), headers: opts.headers || {} });
    return { ok: true, status: 200, json: async () => ({ status: "ok" }) };
  }
  if (url === "/lab/feed") return { ok: true, json: async () => feed };
  if (url.startsWith("/lab/entity?id=")) {
    const id = decodeURIComponent(url.slice("/lab/entity?id=".length));
    const d = entities[id];
    if (d) return { ok: true, status: 200, json: async () => d };
    return { ok: false, status: 404, json: async () => ({ error: "no such entity" }) };
  }
  if (url.startsWith("/lab/log")) return { ok: true, status: 200, json: async () => logPage };
  return { ok: false, status: 404, json: async () => ({}) };
};

window.eval(appJs);
await new Promise((r) => setTimeout(r, 100)); // let the initial refresh() settle
const doc = window.document;

console.log("== observer mode (default, no token) ==");
check(!doc.querySelector("#inbox .decision-card button.approve"),
  "observer mode hides approve/reject buttons");
check(doc.getElementById("composer").style.display === "none",
  "observer mode hides the composer");
check(doc.getElementById("role").textContent === "observing",
  "observing indicator shown");
check(!doc.body.innerHTML.includes("test-operator-token"),
  "token never rendered into the DOM");

console.log("== operator mode (token in localStorage) ==");
window.localStorage.setItem("lab_token", "test-operator-token");
window.eval("render()");
await new Promise((r) => setTimeout(r, 50));
check(!doc.body.innerHTML.includes("test-operator-token"),
  "token still never rendered after login");

console.log("== feed view ==");
const cards = doc.querySelectorAll("#inbox .decision-card");
check(cards.length === feed.inbox.length && cards.length > 0,
  `inbox renders one card per pending decision (${cards.length}/${feed.inbox.length})`);
check(!!doc.querySelector("#inbox .decision-card button.approve"),
  "decision cards have approve/reject buttons (operator)");
check(doc.getElementById("composer").style.display !== "none",
  "composer visible for the operator");
const groups = doc.querySelectorAll("#branches .branch-group");
check(groups.length === feed.branches.length && groups.length > 0,
  `branch groups render (${groups.length}/${feed.branches.length})`);
check(doc.title.startsWith(`(${feed.inbox.length})`),
  `pending count in tab title ("${doc.title}")`);
const blanks = [...doc.querySelectorAll(".entry .text")]
  .filter((n) => !n.textContent.trim());
check(blanks.length === 0, `no entry renders blank (${blanks.length} blank)`);

console.log("== universal id links ==");
check(!!doc.querySelector('a[href^="#entity="]'),
  "feed renders id links into the inspector");
const chips = [...doc.querySelectorAll(".entry .when")];
check(chips.length > 0 && chips.every((n) => n.querySelector('a[href^="#entity="]')),
  `every entry's event-id chip is a link (${chips.length} chips)`);

console.log("== thread view ==");
const entryKind = (e) => e.sentence.includes(" said: ") ? "user"
  : e.sentence.startsWith("Lab replied") ? "lab" : "run";
const richBranch =
  feed.branches.find((b) => new Set(b.entries.map(entryKind)).size >= 2)
  || feed.branches.find((b) => b.entries.length >= 2) || feed.branches[0];
const group = doc.querySelector(`[data-branch-id="${richBranch.branch.id}"]`);
group.click();
await new Promise((r) => setTimeout(r, 50));
check(doc.getElementById("thread-view").hidden === false, "thread view opens on click");
const timeline = doc.querySelectorAll("#timeline .entry");
check(timeline.length === richBranch.entries.length && timeline.length > 0,
  `one-scroll timeline renders all entries (${timeline.length})`);
const kinds = new Set([...timeline].map((n) => n.className));
check(kinds.size >= 2 || timeline.length < 2,
  `timeline interleaves entry kinds (${[...kinds].join(", ")})`);
check(!!doc.getElementById("composer"), "chat composer present in thread view");

// B4: drafts render in the thread view with a preview snippet + link.
doc.getElementById("back").click();
await new Promise((r) => setTimeout(r, 50));
const draftBranch = feed.branches.find((b) => b.entries.some((e) => e.artifact));
if (draftBranch) {
  doc.querySelector(`[data-branch-id="${draftBranch.branch.id}"]`).click();
  await new Promise((r) => setTimeout(r, 50));
  const link = doc.querySelector("#timeline .draft-preview a");
  check(!!link && link.href.includes("/lab/draft?slug="),
    `draft entries in the thread view link to /lab/draft (${link ? link.getAttribute("href") : "none"})`);
  check(!!doc.querySelector("#timeline .draft-preview .snippet"),
    "draft entries carry a preview snippet");
} else {
  check(false, "no branch carries a draft entry to verify");
}

console.log("== decision buttons -> rationale form -> /lab/decision (ADR-026) ==");
doc.getElementById("back").click();
await new Promise((r) => setTimeout(r, 50));
const annotated = feed.inbox.find((d) => (d.annotations || []).length > 0);
const target = annotated || feed.inbox[0];
const firstCard = doc.querySelector(`[data-decision-id="${target.id}"]`);
const decisionId = firstCard.dataset.decisionId;
check(!annotated || !!firstCard.querySelector(".annotations"),
  "annotated decision renders its operator notes");
firstCard.querySelector("button.approve").click();
await new Promise((r) => setTimeout(r, 50));
const form = firstCard.querySelector(".resolve-form");
check(!!form && form.hidden === false,
  "approve opens the optional rationale form, no immediate POST");
check(!posts.some((p) => p.url === "/lab/decision"),
  "nothing POSTs before confirm");
const field = firstCard.querySelector(".resolve-rationale");
if (annotated) {
  const notes = annotated.annotations;
  check(field.value === notes[notes.length - 1].text,
    "rationale prefilled from the most recent annotation");
} else {
  check(field.value === "", "rationale prefilled empty (skippable)");
}
field.value = "ui-check: reason recorded on the resolution event";

console.log("== rationale persists across re-renders (the mobile keyboard case) ==");
// The 3s poll / SSE push re-renders mid-composition; on mobile the soft
// keyboard's viewport churn makes this near-certain on first focus. The open
// form must keep its DOM node (text + focus) until explicit confirm/cancel.
window.eval("render()");
await new Promise((r) => setTimeout(r, 50));
const cardAfter = doc.querySelector(`[data-decision-id="${decisionId}"]`);
const formAfter = cardAfter && cardAfter.querySelector(".resolve-form");
const fieldAfter = cardAfter && cardAfter.querySelector(".resolve-rationale");
check(!!formAfter && formAfter.hidden === false,
  "re-render mid-composition keeps the rationale form open");
check(!!fieldAfter && fieldAfter.value === "ui-check: reason recorded on the resolution event",
  "typed rationale survives the re-render");
check(fieldAfter === field,
  "the textarea keeps its DOM node (focus/keyboard not dismissed)");

firstCard.querySelector("button.confirm").click();
await new Promise((r) => setTimeout(r, 50));
const hit = posts.find((p) => p.url === "/lab/decision");
check(!!hit && hit.body.decision_id === decisionId && hit.body.approved === true
  && hit.body.rationale === "ui-check: reason recorded on the resolution event",
  `confirm POSTs {decision_id: ${decisionId}, approved: true, rationale} to /lab/decision`);
check(!!hit && hit.headers && hit.headers.Authorization === "Bearer test-operator-token",
  "mutation carries the Bearer header");

console.log("== inspector: object entity (#entity=<id>) ==");
const branchId = bundle.branch_id;
window.location.hash = `#entity=${branchId}`;
await new Promise((r) => setTimeout(r, 80));
check(doc.getElementById("entity-view").hidden === false,
  `#entity=${branchId} deep-links into the inspector`);
const ebody = doc.getElementById("entity-body");
check(!!ebody.querySelector(".entity-id"), "inspector shows the entity header");
check(ebody.querySelectorAll("table.fields tr").length > 0,
  "inspector renders the object's fields");
check(!!ebody.querySelector('.rel-row a[href^="#entity="]'),
  "inspector renders relations as links");
check(!!ebody.querySelector("details.raw pre.json"),
  "inspector carries the raw JSON");
check(!!ebody.querySelector(`a[href="#branch=${branchId}"]`),
  "branch entity links back to its thread");

console.log("== inspector: event entity (place in time) ==");
const eventId = bundle.event_id;
window.location.hash = `#entity=${eventId}`;
await new Promise((r) => setTimeout(r, 80));
check(doc.getElementById("entity-view").hidden === false,
  `#entity=${eventId} renders the event view`);
const nav = ebody.querySelector(".entity-nav");
check(!!nav && nav.children.length === 2,
  "event view has prev/next place-in-time navigation");
check((ebody.querySelector(".entity-narration") || {}).textContent?.trim().length > 0,
  "event view narrates a non-blank summary");
check(!!ebody.querySelector("details.raw pre.json"), "event view carries raw event JSON");

console.log("== the full event log (#log) ==");
window.location.hash = "#log";
await new Promise((r) => setTimeout(r, 80));
check(doc.getElementById("log-view").hidden === false, "#log deep-links the event log");
const rows = [...doc.querySelectorAll("#log-body .log-row")];
check(rows.length === logPage.rows.length && rows.length > 0,
  `log renders one row per event (${rows.length}/${logPage.rows.length})`);
check(rows.every((n) => n.querySelector(".text").textContent.trim()),
  "no log row renders blank (unknown kinds fall back to the kind name)");
check(rows.every((n) => n.querySelector('a[href^="#entity="]')),
  "every log row links into the inspector");

console.log("== back to the feed ==");
window.location.hash = "";
await new Promise((r) => setTimeout(r, 80));
check(doc.getElementById("feed-view").hidden === false, "clearing the hash returns to the feed");
check(!!doc.getElementById("about"), "orientation strip present on the feed");

console.log(`\ncheck_ui (jsdom): ${failures.length === 0 ? "PASS" : "FAIL"} (${failures.length} failure(s))`);
process.exit(failures.length === 0 ? 0 : 1);
