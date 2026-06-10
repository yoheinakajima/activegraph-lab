#!/usr/bin/env python3
"""Auth unit tests (2d): bearer-token mutations, public reads, read-only
mode, dev-only /reset, rate limiting. In-process, no persistence, no key.

Run:
    python scripts/test_auth.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def req(base: str, path: str, method: str = "GET", body: dict | None = None,
        token: str | None = None) -> tuple[int, dict]:
    r = urllib.request.Request(base + path, method=method)
    if token is not None:
        r.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body or {}).encode() if method == "POST" else None
    try:
        with urllib.request.urlopen(r, data=data, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def main() -> int:
    import tempfile
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    from server import lab_server

    clear_lab_registry()
    reset_llm_session()
    rt = build_lab(llm_provider=LabMockProvider(),
                   lab_settings=LabSettings(crawl_enabled=False,
                                            drafts_dir=tempfile.mkdtemp()))
    rt.run_until_idle()
    branch = next(b for b in rt.graph.objects(type="branch"))

    lab_server._rt = rt
    lab_server._llm_info = {"mode": "mock", "provider": "mock", "model": None}
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    try:
        print("== read-only mode (token unset) ==")
        os.environ.pop("LAB_OPERATOR_TOKEN", None)
        s, _ = req(base, "/lab/feed")
        check(s == 200, "tokenless GET /lab/feed → 200")
        s, b = req(base, "/chat", "POST", {"branch_id": branch.id, "content": "hi"})
        check(s == 403 and "read-only" in b.get("error", ""),
              f"mutation in read-only mode → 403 ({b.get('error', '')[:40]})")

        print("== token set ==")
        os.environ["LAB_OPERATOR_TOKEN"] = "sekrit-test-token"
        s, _ = req(base, "/chat", "POST", {"branch_id": branch.id, "content": "hi"})
        check(s == 401, "missing bearer → 401")
        s, _ = req(base, "/chat", "POST", {"branch_id": branch.id, "content": "hi"},
                   token="wrong")
        check(s == 403, "wrong bearer → 403")
        s, b = req(base, "/chat", "POST",
                   {"branch_id": branch.id, "content": "what's up?", "user_ref": "mallory"},
                   token="sekrit-test-token")
        check(s == 200, "valid bearer → 200")
        msgs = [m for m in rt.graph.objects(type="comm_message")
                if m.data.get("direction") == "inbound"]
        check(all(m.data.get("sender_ref") == "operator" for m in msgs),
              "client-supplied user_ref ignored — sender is always 'operator'")
        s, _ = req(base, "/lab/feed")
        check(s == 200, "public GET still open with token set")
        s, b = req(base, "/healthz")
        check(s == 200 and "backend" in b and "pending_decisions" in b,
              f"/healthz exposes backend + pending ({b.get('backend')})")

        print("== /reset gating ==")
        os.environ["LAB_ENV"] = "prod"
        s, _ = req(base, "/reset", "POST", {})
        check(s == 404, "/reset in prod → 404 (no override)")
        os.environ["LAB_ENV"] = "dev"

        print("== rate limit ==")
        lab_server._mutation_times.clear()
        codes = []
        for _ in range(31):
            s, _ = req(base, "/lab/decision", "POST",
                       {"decision_id": "decision#999"}, token="sekrit-test-token")
            codes.append(s)
        check(codes[-1] == 429 and 429 not in codes[:30],
              f"31st mutation in a minute → 429 (got {codes[-1]})")
    finally:
        httpd.shutdown()
        lab_server._rt = None
        os.environ.pop("LAB_OPERATOR_TOKEN", None)
        lab_server._mutation_times.clear()

    print(f"\ntest_auth: {'PASS' if not FAILURES else 'FAIL'} ({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
