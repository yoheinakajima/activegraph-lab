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


def build_bundle() -> dict:
    """A real feed plus inspector/log fixtures: boot the lab, leave a promote
    decision pending, chat once, then project /lab/entity and /lab/log the
    same way the server would."""
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    from lab_pack.tools import (activate_branch_fn, complete_task_fn,
                                send_branch_message_fn)
    from server.lab_server import _entity_projection, _feed, _log_page

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
    # ADR-026: annotate one pending decision so the rendered inbox carries an
    # annotation and the resolve form's prefill path is exercised.
    from lab_pack.tools import annotate_decision_fn
    pend = next((d for d in g.objects(type="decision")
                 if d.data.get("status") == "pending"), None)
    if pend is not None:
        annotate_decision_fn(g, pend.id,
                             "UI-check note: recommend approve — evidence chain complete.")
        rt.run_until_idle()
    feed = _feed(rt)

    # Inspector fixtures: a branch, an observation, an artifact, and the
    # branch's creation event — the four shapes the entity view renders.
    branch_id = str(proposed[0].id) if proposed else feed["branches"][0]["branch"]["id"]
    obs = next(iter(g.objects(type="observation")), None)
    art = next(iter(g.objects(type="artifact")), None)
    entities = {}
    for eid in filter(None, (branch_id,
                             str(obs.id) if obs is not None else None,
                             str(art.id) if art is not None else None)):
        entities[eid] = _entity_projection(g, eid)
    event_id = (entities[branch_id] or {}).get("created", {}).get("event_id")
    if event_id:
        entities[event_id] = _entity_projection(g, event_id)
    return {"feed": feed, "entities": entities, "log": _log_page(g, None, 100),
            "branch_id": branch_id, "event_id": event_id}


def static_fallback(bundle: dict) -> None:
    """No jsdom: assert DOM structure statically + the API contract, and say so."""
    print("== check_ui: node/jsdom UNAVAILABLE — static DOM + API contract fallback ==")
    feed = bundle["feed"]
    html = (REPO / "ui" / "index.html").read_text()
    js = (REPO / "ui" / "app.js").read_text()
    for el in ("id=\"inbox\"", "id=\"branches\"", "id=\"timeline\"",
               "id=\"composer\"", "id=\"thread-view\"", "id=\"mission-log\"",
               "id=\"entity-view\"", "id=\"entity-body\"", "id=\"log-view\"",
               "id=\"about\""):
        check(el in html, f"index.html declares {el}")
    check("/lab/feed" in js, "app.js polls /lab/feed")
    check("/lab/decision" in js, "app.js posts decisions to /lab/decision")
    check("resolve-form" in js and "resolve-rationale" in js,
          "app.js wires the optional resolution-rationale form (ADR-026)")
    check("composingIn" in js and ".resolve-form:not([hidden])" in js,
          "app.js freezes the inbox while a rationale is being composed "
          "(mobile keyboard fix)")
    check("annotations" in js, "app.js renders decision annotations (ADR-026)")
    check(all(isinstance(d.get("annotations"), list) for d in feed.get("inbox", [])),
          "feed contract: inbox decisions expose annotations")
    check("/chat" in js, "app.js posts messages to /chat")
    check("/lab/entity" in js, "app.js fetches /lab/entity (the inspector)")
    check("/lab/log" in js, "app.js fetches /lab/log (the full event log)")
    check("linkifyIds" in js and "#entity=" in js, "id linkification wired in code")
    check("decisionCard" in js and "approve" in js, "decision buttons wired in code")
    check(isinstance(feed.get("inbox"), list) and feed["inbox"],
          "feed contract: inbox is a non-empty list")
    check(isinstance(feed.get("branches"), list) and feed["branches"],
          "feed contract: branches is a non-empty list")
    # the projection contract the inspector and log views rely on
    ents = bundle.get("entities") or {}
    check(ents and all(v is not None for v in ents.values()),
          f"entity contract: {len(ents)} sample projections resolved")
    b = ents.get(bundle.get("branch_id")) or {}
    check(bool((b.get("relations_in") or []) or (b.get("relations_out") or [])),
          "entity contract: branch projection carries relations")
    ev = ents.get(bundle.get("event_id")) or {}
    check(ev.get("kind") == "event" and bool(ev.get("summary")),
          "entity contract: event projection has a summary + place in time")
    rows = (bundle.get("log") or {}).get("rows") or []
    check(bool(rows) and all((r.get("summary") or "").strip() for r in rows),
          f"log contract: {len(rows)} rows, no blank summaries")


def main() -> int:
    bundle = build_bundle()
    node = shutil.which("node")
    jsdom_dir = None
    if node:
        for candidate in (REPO / "node_modules", Path("/tmp/node_modules")):
            if (candidate / "jsdom").exists():
                jsdom_dir = candidate
                break
    if node and jsdom_dir:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bundle, f, default=str)
            bundle_file = f.name
        proc = subprocess.run(
            [node, str(REPO / "scripts" / "check_ui.mjs"), bundle_file,
             str(REPO / "ui"), str(jsdom_dir)],
            capture_output=True, text=True, timeout=120)
        print(proc.stdout, end="")
        if proc.stderr.strip():
            print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode
    static_fallback(bundle)
    print(f"\ncheck_ui (static fallback): {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
