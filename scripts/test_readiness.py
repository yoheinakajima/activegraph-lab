#!/usr/bin/env python3
"""Boot/readiness fixtures (ADR-024).

On resumed boot the server used to bind the socket only AFTER the full
replay + drain; as the log grows that exceeds deploy health-grace windows.
The contract pinned here: the socket binds first; /healthz answers from
bind time with ready=false + the boot phase; every other request gets
503 + Retry-After until the drain completes; ready flips when the worker
finishes; a failed boot reports phase=failed with a sanitized error.

The runtime build is substituted with a synthetic slow boot — the HTTP
layer, the worker, the gating, and the phase reporting are the real ones.

Run:
    python scripts/test_readiness.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

os.environ["LAB_LLM_PROVIDER"] = "mock"  # NEVER live in fixtures

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def get(base: str, path: str):
    """Returns (status, headers, json_body)."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, dict(e.headers), {}


def get_raw(base: str, path: str):
    """Returns (status, headers, text) without assuming JSON."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, dict(r.headers), (r.read() or b"").decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), (e.read() or b"").decode("utf-8", "replace")


def post(base: str, path: str, body: dict):
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, dict(e.headers), {}


def reset(lab_server):
    lab_server._rt = None
    lab_server._worker = None
    lab_server._ERRORS.clear()
    lab_server._mutation_times.clear()
    lab_server._BOOT_PHASE["phase"] = "starting"


def main() -> int:
    import tempfile
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    from server import lab_server

    drain_release = threading.Event()

    def slow_build_runtime():
        """A synthetic slow drain: real runtime, gated 'draining' phase."""
        clear_lab_registry()
        reset_llm_session()
        lab_server._BOOT_PHASE["phase"] = "loading"
        rt = build_lab(llm_provider=LabMockProvider(),
                       lab_settings=LabSettings(crawl_enabled=False,
                                                drafts_dir=tempfile.mkdtemp()),
                       create_mission=True)
        lab_server._BOOT_PHASE["phase"] = "draining"
        drain_release.wait(30)  # the synthetic slow drain
        rt.run_until_idle()
        return rt

    real_build = lab_server._build_runtime
    lab_server._build_runtime = slow_build_runtime
    reset(lab_server)

    print("== bind first: the socket answers while the drain is running ==")
    # emulate main(): worker starts, socket binds, boot continues behind it
    lab_server._worker = lab_server._RuntimeWorker()
    lab_server._worker.start()
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        deadline = time.monotonic() + 15
        while lab_server._BOOT_PHASE["phase"] != "draining" \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        s, _, hz = get(base, "/healthz")
        check(s == 200 and hz.get("ready") is False,
              f"healthz answers mid-boot with ready=false ({s})")
        check(hz.get("phase") == "draining",
              f"healthz reports the boot phase ({hz.get('phase')})")
        s, h, body = get(base, "/lab/feed")
        check(s == 503 and h.get("Retry-After") == "5",
              f"reads get 503 + Retry-After during the drain ({s}, {h.get('Retry-After')})")
        check(body.get("phase") == "draining", "503 body names the phase")
        s, h, _ = post(base, "/chat", {"branch_id": "branch#2", "content": "x"})
        check(s == 503 and h.get("Retry-After") == "5",
              f"mutations get 503 + Retry-After during the drain ({s})")
        s, h, _ = post(base, "/mcp", {"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize"})
        check(s == 503, f"MCP gets 503 during the drain ({s})")
        s, _, errs = get(base, "/lab/errors")
        check(s == 200 and "errors" in errs,
              "/lab/errors stays readable during boot (diagnostics never gated)")

        # ADR-030: the front door returns 200 (a minimal starting page) while
        # not-ready, so the platform healthcheck's probe of / stops logging
        # boot failures; other not-ready routes keep their 503.
        s, h, text = get_raw(base, "/")
        check(s == 200 and "text/html" in h.get("Content-Type", "")
              and "starting" in text.lower(),
              f"GET / returns 200 + starting page while booting ({s})")
        check("draining" in text,
              "the starting page names the true boot phase")
        s2, _, _ = get_raw(base, "/posts/anything")
        check(s2 == 503, f"other not-ready routes still 503 ({s2})")

        drain_release.set()
        lab_server._worker.ready.wait(60)
        deadline = time.monotonic() + 10
        while lab_server._BOOT_PHASE["phase"] != "ready" \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        s, _, hz = get(base, "/healthz")
        check(s == 200 and hz.get("ready") is True and hz.get("phase") == "ready",
              f"ready flips when the drain completes ({hz.get('phase')})")
        check(hz.get("event_count", 0) > 0,
              "healthz serves the full projection once ready")
        s, _, feed = get(base, "/lab/feed")
        check(s == 200 and "as_of_event" in feed,
              f"requests flow after readiness ({s})")
    finally:
        httpd.shutdown()
        reset(lab_server)

    print("== failed boot: phase=failed with a sanitized error ==")
    def broken_build_runtime():
        raise RuntimeError("boot exploded at postgres://user:pw@db.internal/lab "
                           "while reading /var/secret/cfg")
    lab_server._build_runtime = broken_build_runtime
    lab_server._worker = lab_server._RuntimeWorker()
    lab_server._worker.start()
    lab_server._worker.ready.wait(30)
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s, _, hz = get(base, "/healthz")
        check(s == 200 and hz.get("phase") == "failed"
              and "RuntimeError" in hz.get("boot_error", ""),
              f"failed boot reported on healthz ({hz.get('boot_error', '')[:50]})")
        check("pw@db.internal" not in json.dumps(hz)
              and "/var/secret/cfg" not in json.dumps(hz),
              "boot error is sanitized (no DSN, no paths)")
        s, _, _ = get(base, "/lab/feed")
        check(s == 503, f"requests stay 503 after a failed boot ({s})")
        s, _, errs = get(base, "/lab/errors")
        check(any(e["kind"] == "boot" for e in errs.get("errors", [])),
              "/lab/errors records the boot failure")
    finally:
        httpd.shutdown()
        reset(lab_server)
        lab_server._build_runtime = real_build

    print(f"\ntest_readiness: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
