#!/usr/bin/env python3
"""Chat-path robustness fixtures (ADR-023).

The production incident: a pg_dump/pg_restore'd log left the events.seq
BIGSERIAL sequence behind the restored rows. Boot appends landed in the
leading gap and succeeded; once nextval reached the restored block, EVERY
durable append died with a UniqueViolation on events_pkey — raised from
store.append inside graph.emit, AFTER the event entered the in-memory log.
send_chat 500'd with a generic error while the message looked committed to
every MCP projection, and the reply drain never started.

Four guarantees are pinned here, all exercised through the REAL server
paths (resumed boot from a restored-shaped log, runtime worker, HTTP, MCP):

  1. LEAF CAUSE — restored-lineage sequence divergence is repaired at boot
     (lab_pack/storage.repair_sequences); chat appends stay durable.
     Postgres section; SKIPs unless LAB_TEST_PG_URL points at a scratch DB.
  2. APPEND FAILURE IS STRUCTURED — when the message append itself fails,
     the response names the exception class + sanitized message (never a
     generic "internal error") and the failure is on /lab/errors.
  3. DEGRADED PATH — post-commit failures (relation upkeep etc.) can NEVER
     fail a request whose message committed: status=reply_pending with the
     committed ids, a chat_path_degraded observation, ring-buffer entries,
     and the reply still produced on the worker.
  4. DISCONNECT-PROOF REPLY — a client that vanishes after POSTing still
     gets its message answered exactly once.
  5. CONNECTION RESILIENCE — serverless Postgres kills idle connections
     (the Neon idle-suspend incident: AdminShutdown on the first write,
     then OperationalError 'the connection is closed' forever). The
     storage adapter reconnects and retries exactly once on
     connection-class errors, recording store_reconnected on the ring
     buffer (never the log); constraint violations are NEVER retried; a
     second failure surfaces structured. Policy is locked everywhere; the
     end-to-end kill-the-backend path needs LAB_TEST_PG_URL.

No live LLM calls (LAB_LLM_PROVIDER=mock is forced), no network.

Run:
    python scripts/test_chat_robustness.py
    LAB_TEST_PG_URL=postgres://...scratch... python scripts/test_chat_robustness.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

os.environ["LAB_LLM_PROVIDER"] = "mock"  # NEVER live in fixtures
MCP_TOKEN = "mcp-test-token-aaaa"
OPERATOR_TOKEN = "operator-test-token-bbbb"

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
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
        "graph, with no central orchestrator.</p>"
    ),
}


def canned_fetch(url, **_kw):
    page = PAGES.get(url.rstrip("/"))
    if page is None:
        return {"url": url, "status": 404, "content": "", "error": "HTTPError: 404"}
    return {"url": url, "status": 200, "content": page}


def write_prod_shaped_log(persist_to: str, drafts_dir: str):
    """Stage A of every section: an app-written lineage whose tail includes
    an existing comm_thread with a discusses relation (live chat happened
    before the 'restore')."""
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (LabMockProvider, LabProviderWrapper,
                              _lab_prompt_bodies, reset_llm_session)
    from lab_pack.settings import LabSettings
    from lab_pack.tools import send_branch_message_fn

    clear_lab_registry()
    reset_llm_session()
    rt = build_lab(
        llm_provider=LabProviderWrapper(LabMockProvider(), max_total=60,
                                        max_per_behavior=10,
                                        prompt_bodies=_lab_prompt_bodies()),
        lab_settings=LabSettings(crawl_page_cap=10, max_claims_per_page=3,
                                 drafts_dir=drafts_dir),
        fetch_handler=canned_fetch,
        persist_to=persist_to,
    )
    rt.run_until_idle()
    g = rt.graph
    branch = next(b for b in g.objects(type="branch"))
    thread_id, msg = send_branch_message_fn(
        g, branch.id, "live chat written before the restore")
    rt.run_until_idle()
    rt.save_state()
    return str(branch.id), str(thread_id)


def resumed_boot():
    """Stage B: a fresh process resumes from the log — module registries
    rebuilt from the restored lineage, runtime owned by the real worker."""
    from lab_pack import clear_lab_registry
    from lab_pack.llm import reset_llm_session
    from server import lab_server

    clear_lab_registry()
    reset_llm_session()
    lab_server._rt = None
    lab_server._worker = None
    lab_server._ERRORS.clear()
    lab_server._mutation_times.clear()
    return lab_server._get_rt()


def serve():
    from server import lab_server
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, base


def mcp_send_chat(base: str, branch_id: str, message: str, rpc_id: int = 1):
    """Returns (http_status, is_error, decoded payload-or-error-text)."""
    req = urllib.request.Request(base + "/mcp", method="POST")
    req.add_header("Authorization", f"Bearer {MCP_TOKEN}")
    req.add_header("Content-Type", "application/json")
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
            "params": {"name": "send_chat",
                       "arguments": {"branch_id": branch_id,
                                     "message": message}}}
    try:
        with urllib.request.urlopen(req, data=json.dumps(body).encode(),
                                    timeout=60) as r:
            resp = json.loads(r.read())
            result = resp.get("result") or {}
            text = (result.get("content") or [{}])[0].get("text", "")
            if result.get("isError"):
                return r.status, True, text
            return r.status, False, json.loads(text)
    except urllib.error.HTTPError as e:
        return e.code, True, (e.read() or b"").decode()


def get_json(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return r.status, json.loads(r.read())


class FlakyAppend:
    """Wraps the live store's append: fail (with the production leaf shape)
    for events selected by `match`."""

    def __init__(self, store, match):
        self.real = store.append
        self.match = match

    def __call__(self, event):
        if self.match(event):
            raise RuntimeError(
                'duplicate key value violates unique constraint "events_pkey" '
                "DETAIL: Key (seq)=(506) already exists. "
                "[simulated restored-lineage divergence; dsn "
                "postgres://lab_user:secret-pw@db.internal:5432/lab]")
        return self.real(event)


def main() -> int:
    from server import lab_server

    os.environ["LAB_MCP_TOKEN"] = MCP_TOKEN
    os.environ["LAB_OPERATOR_TOKEN"] = OPERATOR_TOKEN
    os.environ.pop("LAB_DATABASE_URL", None)
    os.environ.pop("DATABASE_URL", None)

    tmp = Path(tempfile.mkdtemp(prefix="lab-chatrob-"))
    os.environ["ACTIVEGRAPH_DB"] = str(tmp / "lab.sqlite")
    os.environ["ACTIVEGRAPH_MEMORY_DB"] = str(tmp / "mem-app.sqlite")

    print("== restored-shaped lineage: app-written log with an existing "
          "comm_thread + discusses ==")
    branch_id, thread_id = write_prod_shaped_log(str(tmp / "lab.sqlite"),
                                                 str(tmp / "drafts"))
    # the 'restore': the log survives, filesystem side state does not
    os.environ["ACTIVEGRAPH_MEMORY_DB"] = str(tmp / "mem-restored.sqlite")
    rt = resumed_boot()
    from lab_pack.behaviors import _THREAD_TO_BRANCH
    check(_THREAD_TO_BRANCH.get(thread_id) == branch_id,
          f"discusses cache rebuilt from the restored log ({thread_id} -> {branch_id})")
    httpd, base = serve()

    try:
        print("== baseline: send_chat against the existing thread, real "
              "server paths ==")
        s, is_err, out = mcp_send_chat(base, branch_id,
                                       "post-restore baseline message", 1)
        check(s == 200 and not is_err and out.get("status") == "ok",
              f"resumed boot answers on the existing thread ({out.get('status') if isinstance(out, dict) else out})")
        check(out.get("thread_id") == thread_id if isinstance(out, dict) else False,
              "message landed in the pre-restore thread (no duplicate thread)")

        print("== 2: the append failure is structured, never a generic 500 ==")
        store = lab_server._rt.graph.store
        flaky = FlakyAppend(store, lambda e: str(e.type) == "object.created"
                            and (e.payload.get("object") or {}).get("type") == "comm_message")
        store.append = flaky
        try:
            s, is_err, out = mcp_send_chat(base, branch_id,
                                           "this append must fail loudly", 2)
        finally:
            store.append = flaky.real
        check(s == 200 and is_err and "message append failed" in str(out)
              and "RuntimeError" in str(out),
              "MCP append failure → tool error naming the exception class")
        check("secret-pw" not in str(out) and "db.internal" not in str(out)
              and "<url>" in str(out),
              "append-failure message is sanitized (DSN scrubbed)")
        s, errs = get_json(base, "/lab/errors")
        kinds = [e["kind"] for e in errs["errors"]]
        check(s == 200 and "mcp.send_chat.append" in kinds,
              f"/lab/errors recorded the append failure ({kinds[:3]})")
        check(all("secret-pw" not in json.dumps(e) for e in errs["errors"]),
              "ring buffer entries are sanitized")
        # the same failure over POST /chat: structured 500, not 'internal error'
        store.append = flaky
        try:
            req = urllib.request.Request(base + "/chat", method="POST",
                                         data=json.dumps({"branch_id": branch_id,
                                                          "content": "fail loud"}).encode())
            req.add_header("Authorization", f"Bearer {OPERATOR_TOKEN}")
            try:
                urllib.request.urlopen(req, timeout=30)
                check(False, "POST /chat append failure must not 200")
            except urllib.error.HTTPError as e:
                body = json.loads(e.read())
                check(e.code == 500 and "RuntimeError" in body.get("error", "")
                      and body.get("error") != "internal error",
                      f"POST /chat append failure → structured 500 ({body.get('error', '')[:60]})")
        finally:
            store.append = flaky.real

        print("== 3: post-commit failure degrades — never a 500 after a "
              "committed append ==")
        # a branch with no thread yet forces the create-thread path, whose
        # discusses relation is post-commit upkeep; fail exactly that append
        g = lab_server._rt.graph
        fresh_branch = next(b.id for b in g.objects(type="branch")
                            if str(b.id) != branch_id
                            and b.data.get("status") != "archived")
        flaky = FlakyAppend(store, lambda e: str(e.type) == "relation.created"
                            and (e.payload.get("relation") or {}).get("type") == "discusses")
        store.append = flaky
        try:
            s, is_err, out = mcp_send_chat(base, str(fresh_branch),
                                           "degrade after my commit", 3)
        finally:
            store.append = flaky.real
        check(s == 200 and not is_err and out.get("status") == "reply_pending",
              f"degraded post-commit → reply_pending, not an error ({out.get('status') if isinstance(out, dict) else out})")
        check(bool(out.get("message_id")) and bool(out.get("message_event_ids")),
              "degraded response still carries the committed message ids")
        deg = out.get("degraded") or []
        check(any(d.get("kind") == "chat.thread_link" for d in deg),
              f"degraded steps identify the failed upkeep ({[d.get('kind') for d in deg]})")
        msg_obj = g.get_object(out["message_id"])
        check(msg_obj is not None, "the message itself IS committed")
        # the reply was queued fire-and-forget on the worker — it must land
        deadline = time.monotonic() + 30
        late = []
        while time.monotonic() < deadline:
            with lab_server._lock:
                late = [c for c in g.objects(type="comm_response_candidate")
                        if str(c.data.get("message_id")) == out["message_id"]]
            if late:
                break
            time.sleep(0.3)
        check(len(late) == 1,
              f"reply produced on the worker despite the degraded path ({len(late)})")
        with lab_server._lock:
            deg_obs = [o for o in g.objects(type="observation")
                       if (o.data.get("metadata") or {}).get("lab") == "chat_path_degraded"]
        check(len(deg_obs) >= 1 and "chat.thread_link" in deg_obs[-1].data.get("text", ""),
              f"chat_path_degraded observation on the public record ({len(deg_obs)})")
        check("secret-pw" not in json.dumps(deg_obs[-1].data, default=str),
              "degraded observation payload is sanitized (no DSN)")
        s, errs = get_json(base, "/lab/errors")
        check("chat.thread_link" in [e["kind"] for e in errs["errors"]],
              "/lab/errors shows the degraded step")

        print("== 4: disconnect-proof reply ==")
        with lab_server._lock:
            n_cands_before = len([c for c in g.objects(type="comm_response_candidate")])
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                           "params": {"name": "send_chat",
                                      "arguments": {"branch_id": branch_id,
                                                    "message": "answer me even if I hang up"}}})
        host, port = base.replace("http://", "").split(":")
        raw = (f"POST /mcp HTTP/1.1\r\nHost: {host}\r\n"
               f"Authorization: Bearer {MCP_TOKEN}\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n\r\n{body}")
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(raw.encode())
        sock.close()  # the client vanishes before any response
        deadline = time.monotonic() + 30
        mine = []
        while time.monotonic() < deadline:
            with lab_server._lock:
                msgs = [m for m in g.objects(type="comm_message")
                        if m.data.get("content") == "answer me even if I hang up"]
                mine = [c for c in g.objects(type="comm_response_candidate")
                        if msgs and str(c.data.get("message_id")) == str(msgs[0].id)]
            if mine:
                break
            time.sleep(0.3)
        check(len(msgs) == 1, "the disconnected client's message committed")
        check(len(mine) == 1,
              f"reply produced exactly once with the client gone ({len(mine)})")
        check(len([c for c in g.objects(type="comm_response_candidate")]) ==
              n_cands_before + 1, "no duplicate replies from the disconnect")
    finally:
        httpd.shutdown()
        lab_server._rt = None
        lab_server._worker = None
        lab_server._mutation_times.clear()

    print("== 5: store reconnect policy — connection-class only, retry "
          "exactly once ==")
    run_reconnect_policy()

    print("== 1: leaf cause on the real backend (postgres) ==")
    pg_url = os.environ.get("LAB_TEST_PG_URL", "").strip()
    if not pg_url:
        print("  [skip] LAB_TEST_PG_URL not set — run on a postgres scratch "
              "database to exercise the real UniqueViolation leaf")
    else:
        run_postgres_leaf(pg_url, str(tmp))
        print("== 5pg: the Neon idle-suspend kill, end to end ==")
        run_postgres_reconnect(pg_url)

    print(f"\ntest_chat_robustness: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


def run_postgres_leaf(pg_url: str, tmp: str) -> None:
    """The production leaf, end to end: a lineage whose events.seq sequence
    sits behind the rows (what a row-level pg restore produces). Boot must
    realign it (storage.repair_sequences) and chat must stay durable."""
    import psycopg
    from lab_pack import storage
    from server import lab_server

    if storage.store_has_run(pg_url):
        check(False, "LAB_TEST_PG_URL already has a run; use a scratch database")
        return

    branch_id, thread_id = write_prod_shaped_log(pg_url, tmp + "/drafts-pg")
    # the restored-lineage divergence: rows present, sequence behind them —
    # the same physical state a data-only/row-level restore leaves behind
    with psycopg.connect(pg_url, autocommit=True) as conn:
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
        conn.execute("SELECT setval('events_seq_seq', %s)", (max(1, max_seq - 40),))
    fixed = storage.repair_sequences(pg_url)
    check(fixed > 0, f"repair_sequences realigned the diverged sequence (+{fixed})")
    check(storage.repair_sequences(pg_url) == 0, "repair is idempotent (0 when aligned)")

    # diverge again and prove the resumed BOOT repairs it before any append
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("SELECT setval('events_seq_seq', %s)", (max(1, max_seq - 40),))
    os.environ["LAB_DATABASE_URL"] = pg_url
    try:
        rt = resumed_boot()
        check(True, f"resumed boot survives the diverged sequence ({len(rt.graph.events)} events)")
        httpd, base = serve()
        try:
            lab_server._mutation_times.clear()
            for i in range(1, 4):
                s, is_err, out = mcp_send_chat(base, branch_id,
                                               f"post-repair durability check {i}", 100 + i)
                if s != 200 or is_err or out.get("status") not in ("ok", "reply_pending"):
                    check(False, f"send_chat {i} after repair → {s} {out}")
                    break
            else:
                check(True, "3 send_chats after the repair: no 500, no UniqueViolation")
            with psycopg.connect(pg_url, autocommit=True) as conn:
                durable = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            with lab_server._lock:
                in_memory = len(lab_server._rt.graph.events)
            check(durable == in_memory,
                  f"every in-memory event is durable again ({in_memory} == {durable})")
        finally:
            httpd.shutdown()
            lab_server._rt = None
            lab_server._worker = None
            lab_server._mutation_times.clear()
    finally:
        os.environ.pop("LAB_DATABASE_URL", None)


class _DyingStore:
    """Shaped like a URL-target PostgresEventStore (owned connection) whose
    every append raises `exc` — the policy tests need failure shapes, not a
    live backend."""

    class _Source:
        def __init__(self):
            self._owned_conn = object()  # owned, close() best-effort
            self._conn = self._owned_conn

    def __init__(self, exc):
        self._source = self._Source()
        self.calls = 0
        self.exc = exc

    def append(self, event):
        self.calls += 1
        raise self.exc


def run_reconnect_policy() -> None:
    """The retry policy, independent of any backend: connection-class errors
    reconnect + retry exactly once; everything else surfaces untouched."""
    import psycopg
    from lab_pack import storage

    # sqlite backend (the env main() set up): harden is a no-op
    check(storage.harden_store(object()) is False,
          "sqlite backend: harden_store is a no-op")

    # UniqueViolation is NEVER retried — a retried append whose first
    # attempt actually committed must surface, not duplicate (ADR-023)
    recons: list = []
    st = _DyingStore(psycopg.errors.UniqueViolation(
        'duplicate key value violates unique constraint "events_pkey"'))
    check(storage.harden_store(st, url="postgres://scratch.invalid/x",
                               record=recons.append),
          "harden_store arms an owned postgres-shaped store")
    check(storage.harden_store(st, url="postgres://scratch.invalid/x") is True,
          "arming is idempotent (no double-wrap)")
    try:
        st.append(None)
        check(False, "UniqueViolation must surface")
    except psycopg.errors.UniqueViolation:
        pass
    check(st.calls == 1 and not recons,
          f"constraint violation: no retry, no reconnect ({st.calls} attempt)")

    # a connection-class error whose reconnect also fails: the failure
    # surfaces structured (class + message for the ring buffer / response),
    # and no phantom store_reconnected is recorded
    st2 = _DyingStore(psycopg.OperationalError("the connection is closed"))
    storage.harden_store(
        st2, url="postgres://lab@scratch.invalid/x?connect_timeout=2",
        record=recons.append)
    try:
        st2.append(None)
        check(False, "double failure must surface")
    except psycopg.OperationalError:
        pass
    check(st2.calls == 1 and not recons,
          "failed reconnect surfaces; nothing recorded as reconnected")


def run_postgres_reconnect(pg_url: str) -> None:
    """The production incident, end to end on the real backend: the live
    store's server-side backend is terminated (what a Neon idle suspend
    does), and the NEXT chat append must reconnect, retry, succeed — with
    store_reconnected on the ring buffer and nothing in the event log.
    Resumes the lineage run_postgres_leaf left in the scratch database."""
    import psycopg
    from lab_pack import storage
    from server import lab_server

    os.environ["LAB_DATABASE_URL"] = pg_url
    try:
        rt = resumed_boot()
        store = lab_server._rt.graph.store
        check(getattr(store, "_lab_reconnect_armed", False),
              "resumed boot armed reconnect-on-failure on the postgres store")
        httpd, base = serve()
        try:
            lab_server._mutation_times.clear()
            branch_id = str(next(b.id for b in rt.graph.objects(type="branch")))
            # the idle suspend: the server kills the store's backend
            pid = store._source._conn.info.backend_pid
            with psycopg.connect(pg_url, autocommit=True) as conn:
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
            time.sleep(0.5)
            s, is_err, out = mcp_send_chat(
                base, branch_id, "first write after the idle suspend", 201)
            check(s == 200 and not is_err
                  and out.get("status") in ("ok", "reply_pending"),
                  f"first chat after the backend kill succeeds via reconnect "
                  f"({out if is_err else out.get('status')})")
            s, errs = get_json(base, "/lab/errors")
            recon = [e for e in errs["errors"]
                     if e["kind"] == "store_reconnected"]
            check(len(recon) >= 1,
                  f"store_reconnected on the ring buffer "
                  f"({[e['kind'] for e in errs['errors'][:4]]})")
            check(bool(recon) and recon[0]["class"] in
                  ("AdminShutdown", "OperationalError", "InterfaceError"),
                  f"ring entry names the triggering error class "
                  f"({recon[0]['class'] if recon else 'none'})")
            with lab_server._lock:
                in_log = any(
                    "store_reconnected" in json.dumps(e.payload, default=str)
                    for e in lab_server._rt.graph.events)
            check(not in_log,
                  "reconnect is ring-buffer-only — nothing in the event log")
            # durability through the NEW connection: every in-memory event
            # is on disk once the reply drain settles
            deadline = time.monotonic() + 30
            durable = in_memory = -1
            while time.monotonic() < deadline:
                with psycopg.connect(pg_url, autocommit=True) as conn:
                    durable = conn.execute(
                        "SELECT count(*) FROM events").fetchone()[0]
                with lab_server._lock:
                    in_memory = len(lab_server._rt.graph.events)
                if durable == in_memory:
                    break
                time.sleep(0.5)
            check(durable == in_memory,
                  f"every in-memory event is durable via the new connection "
                  f"({in_memory} == {durable})")
        finally:
            httpd.shutdown()
            lab_server._rt = None
            lab_server._worker = None
            lab_server._mutation_times.clear()
    finally:
        os.environ.pop("LAB_DATABASE_URL", None)

    # retry is bounded on the real backend too: an append that keeps
    # failing with a connection-class error reconnects ONCE (a real
    # psycopg.connect against the scratch db), then surfaces
    from activegraph.store.postgres import PostgresEventStore
    st = PostgresEventStore(pg_url, run_id="reconnect-policy-fixture")
    try:
        calls = {"n": 0}

        def dying_append(event):
            calls["n"] += 1
            raise psycopg.OperationalError(
                "the connection is closed [simulated persistent outage]")

        st.append = dying_append
        recons: list = []
        storage.harden_store(st, url=pg_url, record=recons.append)
        try:
            st.append(None)
            check(False, "persistent connection failure must surface")
        except psycopg.OperationalError:
            pass
        check(calls["n"] == 2 and len(recons) == 1,
              f"retried exactly once: {calls['n']} attempts, "
              f"{len(recons)} reconnect(s) recorded")
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
