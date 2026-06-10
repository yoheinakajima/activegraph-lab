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

const [feedFile, uiDir, jsdomDir] = process.argv.slice(2);
const require = createRequire(
  jsdomDir ? path.join(jsdomDir, "/") : import.meta.url);
const { JSDOM } = require("jsdom");

const feed = JSON.parse(readFileSync(feedFile, "utf8"));
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

console.log("== thread view ==");
const richBranch = feed.branches.find((b) => b.entries.length >= 2) || feed.branches[0];
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

console.log("== decision buttons -> /lab/decision ==");
doc.getElementById("back").click();
await new Promise((r) => setTimeout(r, 50));
const firstCard = doc.querySelector("#inbox .decision-card");
const decisionId = firstCard.dataset.decisionId;
firstCard.querySelector("button.approve").click();
await new Promise((r) => setTimeout(r, 50));
const hit = posts.find((p) => p.url === "/lab/decision");
check(!!hit && hit.body.decision_id === decisionId && hit.body.approved === true,
  `approve button POSTs {decision_id: ${decisionId}, approved: true} to /lab/decision`);
check(!!hit && hit.headers && hit.headers.Authorization === "Bearer test-operator-token",
  "mutation carries the Bearer header");

console.log(`\ncheck_ui (jsdom): ${failures.length === 0 ? "PASS" : "FAIL"} (${failures.length} failure(s))`);
process.exit(failures.length === 0 ? 0 : 1);
