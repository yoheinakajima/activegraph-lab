#!/usr/bin/env python3
"""Postgres integration test (1d). SKIPS unless LAB_DATABASE_URL (or
DATABASE_URL — ADR-009 note) is set.

Runs the smoke loop against the native PostgresEventStore, then simulates a
restart (Runtime.load from the same URL) and verifies mode=resumed with the
same graph state. Exit 0 on pass OR skip; 1 on failure.

First-deploy verification (single line, zsh):
    LAB_DATABASE_URL=$LAB_DATABASE_URL python scripts/test_postgres.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))


def main() -> int:
    from lab_pack import storage

    if storage.backend() != "postgres":
        print("test_postgres: SKIP — LAB_DATABASE_URL/DATABASE_URL not set "
              "(run on the deploy target: LAB_DATABASE_URL=... python scripts/test_postgres.py)")
        return 0

    from activegraph import Runtime
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab, load_lab_packs
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    url = storage.store_url()
    if storage.store_has_run(url):
        print("test_postgres: FAIL — store already has a run; use a scratch database")
        return 1

    failures: list[str] = []

    def check(cond, msg):
        print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
        if not cond:
            failures.append(msg)

    pages = {"https://activegraph.ai": (
        "<p>ActiveGraph is an event-sourced agent runtime. Every mutation is "
        "appended to an event log before it changes graph state.</p>")}

    def fetch(url, **_kw):
        page = pages.get(url.rstrip("/"))
        return ({"url": url, "status": 200, "content": page} if page
                else {"url": url, "status": 404, "content": "", "error": "404"})

    print("== boot fresh on postgres ==")
    clear_lab_registry()
    reset_llm_session()
    import tempfile
    rt = build_lab(llm_provider=LabMockProvider(),
                   lab_settings=LabSettings(drafts_dir=tempfile.mkdtemp()),
                   fetch_handler=fetch, persist_to=url)
    rt.run_until_idle()
    rt.save_state()
    n_events = len(rt.graph.events)
    n_branches = len(rt.graph.objects(type="branch"))
    pending = [d.id for d in rt.graph.objects(type="decision")
               if d.data.get("status") == "pending"]
    check(n_events > 50, f"fresh boot produced events ({n_events})")
    check(n_branches >= 1, f"branches exist ({n_branches})")
    check(storage.store_has_run(url), "store reports a run (resume will trigger)")

    print("== restart: Runtime.load from the same DATABASE_URL ==")
    clear_lab_registry()
    rt2 = Runtime.load(url, llm_provider=LabMockProvider())
    load_lab_packs(rt2)
    check(len(rt2.graph.events) >= n_events,
          f"resumed event count >= fresh ({len(rt2.graph.events)} >= {n_events})")
    check(len(rt2.graph.objects(type="branch")) == n_branches,
          "branch count survives the restart")
    pending2 = [d.id for d in rt2.graph.objects(type="decision")
                if d.data.get("status") == "pending"]
    check(pending2 == pending, "pending decisions survive the restart")

    print(f"\ntest_postgres: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
