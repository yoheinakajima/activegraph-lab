#!/usr/bin/env python3
"""End-to-end smoke: the regression bar for every session.

Boots build_lab() against fixture content (no network, no API key), runs the
full loop — ingest → plan → activate a branch → work → gap → completion →
interpret → decision pending — plus chat, the LLM budget path, and the live
/lab/feed endpoint. Exits 0 on success, nonzero on any failure.

Run:
    python scripts/smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    mark = "ok" if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        FAILURES.append(msg)


PAGES = {
    "https://activegraph.ai": (
        '<a href="/docs">docs</a>'
        "<p>ActiveGraph is an event-sourced agent runtime. Every mutation is "
        "appended to an event log before it changes graph state, so any agent "
        "decision can be replayed and audited.</p>"
    ),
    "https://activegraph.ai/docs": (
        "<p>Behaviors fire automatically when matching objects appear in the "
        "graph, with no central orchestrator. Packs compose through graph "
        "state, not function calls.</p>"
    ),
}


def canned_fetch(url: str, **_kw) -> dict:
    page = PAGES.get(url.rstrip("/"))
    if page is None:
        return {"url": url, "status": 404, "content": "", "error": "HTTPError: 404"}
    return {"url": url, "status": 200, "content": page}


def main() -> int:
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (
        LabMockProvider, LabProviderWrapper, llm_usage,
        reset_llm_session, reset_llm_run_counters, _lab_prompt_bodies,
    )
    from lab_pack.settings import LabSettings
    from lab_pack.tools import (
        activate_branch_fn, complete_task_fn, send_branch_message_fn,
    )
    from server import lab_server

    print("== smoke: boot build_lab() against fixture content ==")
    clear_lab_registry()
    reset_llm_session()
    tmp = tempfile.mkdtemp(prefix="lab-smoke-")
    settings = LabSettings(crawl_page_cap=10, max_claims_per_page=3,
                           drafts_dir=str(Path(tmp) / "drafts"))
    rt = build_lab(llm_provider=LabProviderWrapper(
                       LabMockProvider(), max_total=60, max_per_behavior=10,
                       prompt_bodies=_lab_prompt_bodies()),
                   lab_settings=settings, fetch_handler=canned_fetch)
    rt.run_until_idle()
    g = rt.graph

    def lab_obs(kind):
        return [o for o in g.objects(type="observation")
                if (o.data.get("metadata") or {}).get("lab") == kind]

    print("== ingest -> plan ==")
    claims = lab_obs("site_claim")
    proposed = [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]
    check(len(claims) >= 3, f"site claims extracted ({len(claims)})")
    check(len(proposed) >= 2, f"branches proposed ({len(proposed)})")
    check(all((b.data.get('metadata') or {}).get("reasoning") for b in proposed),
          "every proposal carries narrated reasoning")

    print("== seed branch -> work -> gap ==")
    gaps = lab_obs("capability_gap")
    check(len(gaps) >= 1, f"capability gap recorded for the seed branch ({len(gaps)})")

    print("== activate one proposal -> work -> completion -> interpret -> decision ==")
    reset_llm_run_counters()
    target = proposed[0]
    activate_branch_fn(g, target.id)
    rt.run_until_idle()
    tasks = [t for t in g.objects(type="task")
             if (t.data.get("metadata") or {}).get("lab_branch_id") == target.id]
    check(len(tasks) == 1, f"task dispatched for activated branch ({len(tasks)})")
    reset_llm_run_counters()
    complete_task_fn(g, tasks[0].id, "Smoke worker: verified the claim against the runtime.", True)
    rt.run_until_idle()
    pending = [d for d in g.objects(type="decision")
               if d.data.get("subject_ref") == target.id and d.data.get("status") == "pending"]
    check(len(pending) == 1, "promote decision is pending (not auto-approved)")
    check(g.get_object(target.id).data.get("status") == "interpreting",
          "branch reached interpreting")

    print("== chat with event-horizon stamp ==")
    reset_llm_run_counters()
    _, msg = send_branch_message_fn(g, target.id, "what's the state here?")
    rt.run_until_idle()
    cands = [c for c in g.objects(type="comm_response_candidate")
             if c.data.get("message_id") == msg.id]
    check(len(cands) == 1 and "as of event" in cands[0].data.get("content", ""),
          "stamped answer produced")

    print("== editorial discipline (ADR-014): seeded findings -> ONE digest ==")
    findings = [o for o in g.objects(type="observation")
                if (o.data.get("metadata") or {}).get("finding")]
    drafts = [a for a in g.objects(type="artifact") if a.data.get("kind") == "blog_draft"]
    pub_pending = [d for d in g.objects(type="decision")
                   if d.data.get("kind") == "publish" and d.data.get("status") == "pending"]
    check(len(findings) >= 5, f"seeded findings queued ({len(findings)})")
    check(len(drafts) >= 1, f"digest drafted from accumulated findings ({len(drafts)})")
    check(len(drafts) < len(findings),
          f"no fire-per-finding: {len(drafts)} draft(s) from {len(findings)} findings")
    digest_meta = drafts[0].data.get("metadata") or {} if drafts else {}
    check(digest_meta.get("post_kind") == "note",
          f"digest draft is post_kind=note ({digest_meta.get('post_kind')})")
    check(len(digest_meta.get("finding_ids") or []) >= 3,
          f"digest covers >=3 findings ({len(digest_meta.get('finding_ids') or [])})")
    check(all(a.data.get("status") == "draft" for a in drafts),
          "every draft is gated (status=draft, nothing published)")
    check(len(pub_pending) >= 1, f"publish decisions pending ({len(pub_pending)})")
    check(all("[^" in (a.data.get("content") or "") for a in drafts),
          "every draft carries evidence footnotes")
    check(all("*Provenance:*" in (a.data.get("content") or "") for a in drafts),
          "every draft carries a provenance block")
    files = list(Path(settings.drafts_dir).glob("*.md"))
    check(len(files) >= 1, f"drafts mirrored to disk ({len(files)})")

    main_run_llm_calls = llm_usage()["total"]

    print("== budget-exhausted path ==")
    from activegraph import Graph, Runtime
    from packs.core import pack as core_pack, CoreSettings
    from lab_pack import pack as lab_pack_obj
    reset_llm_session()
    tight = LabProviderWrapper(LabMockProvider(), max_total=1,
                               prompt_bodies=_lab_prompt_bodies())
    g2 = Graph()
    rt2 = Runtime(g2, llm_provider=tight)
    rt2.load_pack(core_pack, settings=CoreSettings())
    rt2.load_pack(lab_pack_obj, settings=LabSettings(crawl_enabled=False))
    for i in range(2):
        g2.add_object("observation", {
            "text": f"Claim number {i}: the runtime replays every event deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": None},
        })
    rt2.run_until_idle()
    budget_obs = [o for o in g2.objects(type="observation")
                  if (o.data.get("metadata") or {}).get("lab") == "llm_budget"]
    check(len(budget_obs) == 1, "budget exhaustion recorded exactly once, run stopped cleanly")
    reset_llm_session()

    print("== daily cap (7b) ==")
    from lab_pack.llm import _LLM_STATE, sync_daily_budget
    daily = LabProviderWrapper(LabMockProvider(), max_daily=0,
                               prompt_bodies=_lab_prompt_bodies())
    g3 = Graph()
    rt3 = Runtime(g3, llm_provider=daily)
    rt3.load_pack(core_pack, settings=CoreSettings())
    rt3.load_pack(lab_pack_obj, settings=LabSettings(crawl_enabled=False))
    g3.add_object("observation", {
        "text": "Daily-capped claim: replay is deterministic in this runtime.",
        "confidence": 0.7, "category": "fact",
        "metadata": {"lab": "site_claim", "mission_id": None},
    })
    rt3.run_until_idle()
    daily_obs = [o for o in g3.objects(type="observation")
                 if (o.data.get("metadata") or {}).get("lab") == "llm_budget"]
    check(len(daily_obs) == 1 and "daily cap" in daily_obs[0].data.get("text", ""),
          "daily cap exhaustion recorded; lab idles until UTC reset")
    # The runtime logs llm.requested BEFORE the provider runs, so even a
    # budget-blocked attempt is in the log — the cap survives restarts and
    # cannot be reset by bouncing the process.
    check(sync_daily_budget(rt3) >= 1,
          f"daily count rebuilt from the log incl. blocked attempts ({_LLM_STATE['daily_used']})")
    reset_llm_session()

    print("== feed endpoint coherence ==")
    lab_server._rt = rt           # no persistence in smoke → thread-safe to serve
    lab_server._llm_info = {"mode": "mock", "provider": "mock", "model": None}
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/lab/feed", timeout=10) as r:
            feed = json.loads(r.read())
        check(set(feed) >= {"as_of_event", "mission", "inbox", "branches", "mission_entries"},
              "feed JSON has the expected shape")
        check(len(feed["inbox"]) >= 1, f"inbox holds pending decisions ({len(feed['inbox'])})")
        check(all(d.get("evidence") for d in feed["inbox"]),
              "every inbox decision has evidence attached")
        all_entries = feed["mission_entries"] + [e for b in feed["branches"] for e in b["entries"]]
        check(len(all_entries) > 5, f"feed entries present ({len(all_entries)})")
        check(all((e.get("sentence") or "").strip() for e in all_entries),
              "no feed entry renders blank")

        print("== blog SSR (ADR-013) ==")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            home = r.read().decode()
        check("Nothing is published yet" in home and "/lab" in home,
              "pre-publish empty state explains the site and links to /lab")
        from lab_pack.tools import approve_decision_fn
        approve_decision_fn(g, pub_pending[0].id, True, "smoke: publish one post")
        rt.run_until_idle()
        published = [a for a in g.objects(type="artifact")
                     if a.data.get("kind") == "blog_draft"
                     and a.data.get("status") == "published"]
        check(len(published) == 1, "approved publish decision published the artifact")
        slug = (published[0].data.get("metadata") or {}).get("slug")
        check(bool((published[0].data.get("metadata") or {}).get("published_at")),
              "published artifact carries published_at")
        check(any(str(e.type) == "artifact.published" for e in g.events),
              "artifact.published event in the log")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            home = r.read().decode()
        check(f"/posts/{slug}" in home, "blog index lists the published post")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/posts/{slug}", timeout=10) as r:
            post = r.read().decode()
        check("Show the work" in post and "Evidence" in post,
              "post page renders the provenance subgraph (Show the work)")
        check("#branch=" in post, "post provenance links into the /lab thread view")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/feed.xml", timeout=10) as r:
            feed_xml = r.read().decode()
        check(f"/posts/{slug}" in feed_xml and "<rss" in feed_xml,
              "RSS at /feed.xml lists the published post")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/lab", timeout=10) as r:
            lab_html = r.read().decode()
        check("app.js" in lab_html, "the notebook UI is served at /lab")

        # 2d: tokenless read succeeds (above); tokenless mutation must fail.
        import os as _os
        import urllib.error as _ue
        _os.environ.pop("LAB_OPERATOR_TOKEN", None)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/chat", method="POST",
            data=json.dumps({"branch_id": target.id, "content": "x"}).encode())
        try:
            urllib.request.urlopen(req, timeout=10)
            check(False, "tokenless mutation must not succeed")
        except _ue.HTTPError as e:
            check(e.code in (401, 403), f"tokenless mutation refused ({e.code})")
    finally:
        httpd.shutdown()
        lab_server._rt = None

    print(f"\nsmoke: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s)) | llm calls in main run: {main_run_llm_calls}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
