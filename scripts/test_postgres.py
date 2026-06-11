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

    print("== reconnect-on-failure: serverless postgres kills idle "
          "connections (ADR-009/023) ==")
    import datetime
    import uuid

    import psycopg
    from activegraph.core.event import Event

    store = rt2.graph.store
    reconnects: list[str] = []
    check(storage.harden_store(
              store, url=url,
              on_reconnect=lambda exc: reconnects.append(type(exc).__name__)),
          "harden_store wraps the postgres store")

    def probe_event() -> Event:
        return Event(id=f"evt_fixture_{uuid.uuid4().hex[:10]}",
                     type="lab.fixture_probe", payload={"fixture": "reconnect"},
                     timestamp=datetime.datetime.now(
                         datetime.timezone.utc).isoformat())

    def kill_backend() -> None:
        """The Neon idle-suspend shape: the server terminates our backend;
        the client only notices on its next statement (AdminShutdown)."""
        pid = store._source._conn.info.backend_pid
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))

    kill_backend()
    ev = probe_event()
    store.append(ev)
    check(store.get_event(ev.id) is not None,
          "append after a server-side kill reconnected and committed")
    check(len(reconnects) == 1 and reconnects[-1] in ("AdminShutdown",
                                                      "OperationalError"),
          f"reconnect recorded the triggering error class ({reconnects})")

    n_before = len(reconnects)
    dup = probe_event()
    store.append(dup)
    try:
        store.append(dup)  # same (id, run_id)
        check(False, "duplicate append must raise UniqueViolation")
    except psycopg.errors.UniqueViolation:
        check(True, "constraint violation surfaces immediately")
    except Exception as e:  # noqa: BLE001 — the class IS the assertion
        check(False, f"duplicate append raised {type(e).__name__}, "
                     "not UniqueViolation")
    check(len(reconnects) == n_before,
          "constraint violation did NOT trigger a reconnect")

    kill_backend()
    real_connect = psycopg.connect

    def refuse(*_a, **_kw):
        raise psycopg.OperationalError(
            "connection refused [simulated unreachable database]")

    psycopg.connect = refuse
    try:
        store.append(probe_event())
        check(False, "double failure must surface, not retry forever")
    except psycopg.OperationalError as e:
        check(True, f"second failure surfaces structured ({type(e).__name__})")
    finally:
        psycopg.connect = real_connect
    ev = probe_event()
    store.append(ev)
    check(store.get_event(ev.id) is not None,
          "store recovers on the next operation once the database is back")

    print(f"\ntest_postgres: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
