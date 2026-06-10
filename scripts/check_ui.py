#!/usr/bin/env python3
"""Render verification for the notebook feed without a browser (C1).

Boots the lab in-process (mock LLM, canned pages), drives the loop until the
feed holds pending decisions, drafts, and chat entries, then renders ui/ in
jsdom (node) against that real /lab/feed JSON and asserts the DOM: inbox
cards, branch groups, interleaved thread timeline, decision buttons wired to
/lab/decision.

If node or jsdom is unavailable, falls back to static DOM-structure +
API-contract assertions and SAYS SO in the output.

Run:
    python scripts/check_ui.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def build_feed() -> dict:
    """A real feed: boot the lab, leave a promote decision pending, chat once."""
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    from lab_pack.tools import (activate_branch_fn, complete_task_fn,
                                send_branch_message_fn)
    from server.lab_server import _feed

    clear_lab_registry()
    reset_llm_session()
    tmp = tempfile.mkdtemp(prefix="lab-ui-")
    pages = {"https://activegraph.ai": (
        "<p>ActiveGraph is an event-sourced agent runtime. Every mutation is "
        "appended to an event log before it changes graph state, so any agent "
        "decision can be replayed and audited.</p>")}

    def fetch(url, **_kw):
        page = pages.get(url.rstrip("/"))
        return ({"url": url, "status": 200, "content": page} if page
                else {"url": url, "status": 404, "content": "", "error": "404"})

    rt = build_lab(llm_provider=LabMockProvider(),
                   lab_settings=LabSettings(drafts_dir=str(Path(tmp) / "drafts")),
                   fetch_handler=fetch)
    rt.run_until_idle()
    g = rt.graph
    proposed = [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]
    if proposed:
        activate_branch_fn(g, proposed[0].id)
        rt.run_until_idle()
        tasks = [t for t in g.objects(type="task")
                 if (t.data.get("metadata") or {}).get("lab_branch_id") == proposed[0].id]
        if tasks:
            complete_task_fn(g, tasks[0].id, "UI-check worker: verified.", True)
            rt.run_until_idle()
        send_branch_message_fn(g, proposed[0].id, "what's the state here?")
        rt.run_until_idle()
    return _feed(rt)


def static_fallback(feed: dict) -> None:
    """No jsdom: assert DOM structure statically + the API contract, and say so."""
    print("== check_ui: node/jsdom UNAVAILABLE — static DOM + API contract fallback ==")
    html = (REPO / "ui" / "index.html").read_text()
    js = (REPO / "ui" / "app.js").read_text()
    for el in ("id=\"inbox\"", "id=\"branches\"", "id=\"timeline\"",
               "id=\"composer\"", "id=\"thread-view\"", "id=\"mission-log\""):
        check(el in html, f"index.html declares {el}")
    check("/lab/feed" in js, "app.js polls /lab/feed")
    check("/lab/decision" in js, "app.js posts decisions to /lab/decision")
    check("/chat" in js, "app.js posts messages to /chat")
    check("decisionCard" in js and "approve" in js, "decision buttons wired in code")
    check(isinstance(feed.get("inbox"), list) and feed["inbox"],
          "feed contract: inbox is a non-empty list")
    check(isinstance(feed.get("branches"), list) and feed["branches"],
          "feed contract: branches is a non-empty list")


def main() -> int:
    feed = build_feed()
    node = shutil.which("node")
    jsdom_dir = None
    if node:
        for candidate in (REPO / "node_modules", Path("/tmp/node_modules")):
            if (candidate / "jsdom").exists():
                jsdom_dir = candidate
                break
    if node and jsdom_dir:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(feed, f, default=str)
            feed_file = f.name
        proc = subprocess.run(
            [node, str(REPO / "scripts" / "check_ui.mjs"), feed_file,
             str(REPO / "ui"), str(jsdom_dir)],
            capture_output=True, text=True, timeout=120)
        print(proc.stdout, end="")
        if proc.stderr.strip():
            print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    static_fallback(feed)
    print(f"\ncheck_ui (static fallback): {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
