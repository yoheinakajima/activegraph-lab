#!/usr/bin/env python3
"""Public-safety audit (3a/3b, ADR-011): the whole event log, every object,
the feed JSON, captured boot output, and error paths must contain ZERO
traces of the secrets in the environment. LAB_DATABASE_URL and DATABASE_URL
are credentials too.

Sentinels are planted in the env, a full loop runs (chat, decision, fetch
failure, LLM anomaly, drafts), everything serializable is grepped.

Run:
    python scripts/test_public_safety.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import os

SENTINELS = {
    "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-vQ9zT3xK7w",
    "LAB_OPERATOR_TOKEN": "tok-SENTINEL-pL2mR8dN4c",
    "LAB_MCP_TOKEN": "mcp-SENTINEL-qW7vJ3hF9e",
    "DATABASE_URL": "postgres://sentinel_user:pw-SENTINEL-aB5xY1@db.sentinel.internal:5432/lab",
    "LAB_DATABASE_URL": "postgres://lab_sentinel_user:pw-SENTINEL-mK6tE9@db.lab-sentinel.internal:5432/lab",
}

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def main() -> int:
    saved = {k: os.environ.get(k) for k in SENTINELS}
    os.environ.update(SENTINELS)
    # Force mock so the sentinel "API key" is never sent anywhere.
    os.environ["LAB_LLM_PROVIDER"] = "mock"
    try:
        return _run()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("LAB_LLM_PROVIDER", None)


def _run() -> int:
    from lab_pack import clear_lab_registry, storage
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (LabProviderWrapper, LabMockProvider,
                              reset_llm_session, _lab_prompt_bodies,
                              select_lab_provider)
    from lab_pack.settings import LabSettings
    from lab_pack.tools import (activate_branch_fn, approve_decision_fn,
                                complete_task_fn, send_branch_message_fn)
    from server.lab_server import _feed

    clear_lab_registry()
    reset_llm_session()
    boot_log = io.StringIO()

    pages = {"https://activegraph.ai": (
        "<p>ActiveGraph is an event-sourced agent runtime. Every mutation is "
        "appended to an event log before it changes graph state.</p>")}

    def fetch(url, **_kw):
        page = pages.get(url.rstrip("/"))
        if page is None:  # error path: a fetch failure observation
            return {"url": url, "status": 403, "content": "", "error": "HTTPError: 403 Forbidden"}
        return {"url": url, "status": 200, "content": page}

    with contextlib.redirect_stdout(boot_log):
        # The same calls the server boot makes — captured and audited.
        provider, info = select_lab_provider(settings=LabSettings())
        print(f"LLM: mode={info['mode']} provider={info['provider']}")
        print(f"boot: backend={storage.backend()}")
        # IMPORTANT: persist_to=None — the sentinel LAB_DATABASE_URL/
        # DATABASE_URL point nowhere; backend() still reads them, which is
        # exactly what we audit.
        rt = build_lab(
            # max_total=4: under ADR-014 the digest drafts once, so the
            # session makes few LLM calls — 4 leaves plan one proposal and
            # still exhausts mid-run (the paused-boot finding added a call).
            llm_provider=LabProviderWrapper(LabMockProvider(), max_total=4,
                                            prompt_bodies=_lab_prompt_bodies()),
            lab_settings=LabSettings(drafts_dir=tempfile.mkdtemp()),
            fetch_handler=fetch)
        rt.run_until_idle()
        g = rt.graph
        # error paths: failed fetch, budget exhaustion, chat, full lifecycle
        from lab_pack.tools import request_crawl_fn
        mission = g.objects(type="mission")[0]
        request_crawl_fn(g, mission.id, "https://activegraph.ai/forbidden")
        rt.run_until_idle()
        branch = next(b for b in g.objects(type="branch") if b.data.get("status") == "proposed")
        activate_branch_fn(g, branch.id)
        rt.run_until_idle()
        tasks = [t for t in g.objects(type="task")
                 if (t.data.get("metadata") or {}).get("lab_branch_id") == branch.id]
        complete_task_fn(g, tasks[0].id, "verified.", True)
        rt.run_until_idle()
        send_branch_message_fn(g, branch.id, "status?")
        rt.run_until_idle()
        pend = [d for d in g.objects(type="decision") if d.data.get("status") == "pending"]
        if pend:
            approve_decision_fn(g, pend[0].id, True, "audit pass")
            rt.run_until_idle()

    # ── MCP surface (ADR-016): tool outputs join the audited corpus too ─────
    import threading
    from server import mcp as mcp_mod

    def mcp_call(method, params=None):
        _, resp = mcp_mod.handle_post(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                        "params": params or {}}).encode(),
            get_rt=lambda: rt, lock=threading.Lock(),
            run_on_worker=lambda fn, *_t: fn(rt), rate_limited=lambda: False)
        return resp

    mcp_outputs = [mcp_call("initialize", {"protocolVersion": "2025-06-18"})]
    for name, args in (("get_status", {}), ("get_feed", {}),
                       ("get_branch", {"branch_id": str(branch.id)}),
                       ("get_pending_decisions", {}), ("list_posts", {}),
                       ("list_seams", {}),
                       ("send_chat", {"branch_id": str(branch.id),
                                      "message": "audit: anything secret in here?"})):
        mcp_outputs.append(mcp_call("tools/call", {"name": name, "arguments": args}))

    # ── URL-token path (ADR-016 amendment): the token rides in the URL, so
    # the HTTP surface around it joins the audit — responses, the wrong-token
    # error path, and everything the server prints while handling them.
    import urllib.error
    import urllib.request
    from http.server import HTTPServer
    from server import lab_server

    http_log = io.StringIO()
    lab_server._rt = rt
    lab_server._llm_info = {"mode": "mock", "provider": "mock", "model": None}
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url_outputs = []
    try:
        with contextlib.redirect_stdout(http_log), contextlib.redirect_stderr(http_log):
            for body in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "send_chat",
                            "arguments": {"branch_id": str(branch.id),
                                          "message": "url-token audit ping"}}},
            ):
                req = urllib.request.Request(
                    f"{base}/mcp/{os.environ['LAB_MCP_TOKEN']}", method="POST",
                    data=json.dumps(body).encode())
                with urllib.request.urlopen(req, timeout=60) as r:
                    url_outputs.append(json.loads(r.read()))
            req = urllib.request.Request(f"{base}/mcp/wrong-token-zz", method="POST",
                                         data=b"{}")
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as e:
                url_outputs.append({"status": e.code,
                                    "body": (e.read() or b"").decode()})

        # ── OAuth flow (ADR-017): DCR → authorize → PKCE → token →
        # tools/call, end to end over HTTP. The 302 Location and the /token
        # response are the intended delivery channels; everything ELSE the
        # flow shows the public (metadata, the authorize page, error bodies,
        # the OAuth-borne chat's events) joins the corpus, and the minted
        # code/access/refresh tokens become sentinels that must be absent
        # from it — as must any other LAB_MCP_TOKEN-derived value.
        import base64
        import hashlib
        import secrets as _secrets
        from urllib.parse import parse_qs as _pq
        from urllib.parse import urlencode, urlparse as _up
        from server import oauth

        oauth_outputs = []
        oauth_secrets = {}
        with contextlib.redirect_stdout(http_log), contextlib.redirect_stderr(http_log):
            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    return r.read().decode()

            def post(path, data: bytes, ctype: str):
                req = urllib.request.Request(base + path, method="POST", data=data)
                req.add_header("Content-Type", ctype)
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return r.status, dict(r.headers), r.read().decode()
                except urllib.error.HTTPError as e:
                    return e.code, dict(e.headers), (e.read() or b"").decode()

            oauth_outputs.append(get("/.well-known/oauth-authorization-server"))
            oauth_outputs.append(get("/.well-known/oauth-protected-resource/mcp"))
            cb = "https://claude.ai/api/mcp/auth_callback"
            s, _, body = post("/register",
                              json.dumps({"redirect_uris": [cb]}).encode(),
                              "application/json")
            oauth_outputs.append(body)
            client_id = json.loads(body)["client_id"]
            verifier = base64.urlsafe_b64encode(_secrets.token_bytes(32)).rstrip(b"=").decode()
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            auth_q = {"response_type": "code", "client_id": client_id,
                      "redirect_uri": cb, "code_challenge": challenge,
                      "code_challenge_method": "S256", "state": "audit-state"}
            oauth_outputs.append(get("/authorize?" + urlencode(auth_q)))

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            req = urllib.request.Request(
                base + "/authorize", method="POST",
                data=urlencode({**auth_q,
                                "token": os.environ["LAB_MCP_TOKEN"]}).encode())
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=10)
                loc = resp.headers.get("Location", "")
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location", "")
            oauth_secrets["oauth_code"] = (_pq(_up(loc).query).get("code") or [""])[0]
            s, _, body = post("/token", urlencode({
                "grant_type": "authorization_code",
                "code": oauth_secrets["oauth_code"], "code_verifier": verifier,
                "client_id": client_id, "redirect_uri": cb}).encode(),
                "application/x-www-form-urlencoded")
            tok = json.loads(body)
            oauth_secrets["oauth_access_token"] = tok["access_token"]
            oauth_secrets["oauth_refresh_token"] = tok["refresh_token"]
            # the OAuth bearer drives a chat: its events join the log corpus
            req = urllib.request.Request(
                base + "/mcp", method="POST",
                data=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                 "params": {"name": "send_chat",
                                            "arguments": {"branch_id": str(branch.id),
                                                          "message": "oauth audit ping"}}}).encode())
            req.add_header("Authorization", f"Bearer {tok['access_token']}")
            with urllib.request.urlopen(req, timeout=60) as r:
                oauth_outputs.append(json.loads(r.read()))
            # error paths join the corpus: wrong password, bad grant
            s, _, body = post("/authorize",
                              urlencode({**auth_q, "token": "wrong-password"}).encode(),
                              "application/x-www-form-urlencoded")
            oauth_outputs.append({"status": s, "body": body})
            s, _, body = post("/token", urlencode({
                "grant_type": "authorization_code", "code": "garbage.garbage",
                "code_verifier": verifier, "client_id": client_id,
                "redirect_uri": cb}).encode(),
                "application/x-www-form-urlencoded")
            oauth_outputs.append({"status": s, "body": body})
        oauth_secrets["oauth_signing_key_hex"] = \
            oauth.derive_key(os.environ["LAB_MCP_TOKEN"]).hex()
    finally:
        httpd.shutdown()
        lab_server._rt = None
        lab_server._mutation_times.clear()

    # ── the corpus: every event payload, every object, feed JSON, boot log ──
    # The /lab/entity and /lab/log projections join it too: one entity page
    # per object and the FULL log summary listing (pure projections, but the
    # audit checks the outputs, not the intent).
    from server.lab_server import _entity_projection, _log_page
    corpus = {
        "events": [{"type": str(e.type), "actor": str(e.actor),
                    "payload": e.payload} for e in g.events],
        "objects": [{"id": str(o.id), "type": str(o.type), "data": o.data}
                    for o in g.all_objects()],
        "feed": _feed(rt),
        "entity_pages": [_entity_projection(g, str(o.id))
                         for o in g.all_objects()],
        "log_rows": _log_page(g, None, len(g.events)),
        "mcp": mcp_outputs,
        "mcp_url_path": url_outputs,
        "oauth": oauth_outputs,
        "http_log": http_log.getvalue(),
        "boot_log": boot_log.getvalue(),
    }
    blob = json.dumps(corpus, default=str)

    print(f"== sentinel audit over {len(g.events)} events, "
          f"{len(g.all_objects())} objects, the feed, the MCP surface, "
          "the OAuth flow, and the boot log ==")
    chat_out = (mcp_outputs[-1].get("result") or {})
    check(not chat_out.get("isError", True),
          "MCP send_chat path exercised (reply produced, joins the corpus)")
    check(len(url_outputs) == 3
          and (url_outputs[0].get("result") or {}).get("protocolVersion")
          and url_outputs[-1].get("status") == 401,
          "URL-token path exercised over HTTP (initialize, send_chat, wrong-token 401)")
    check(all(oauth_secrets.values())
          and not ((oauth_outputs[-3].get("result") or {}).get("isError", True)),
          "OAuth flow exercised end to end (code, tokens minted; "
          "OAuth-borne send_chat replied)")
    for name, sentinel in SENTINELS.items():
        check(sentinel not in blob, f"{name} sentinel absent from the public corpus")
    # ADR-017: nothing the OAuth flow mints — the code, both tokens, their
    # signature halves, or the derived signing key — may reach the corpus.
    for name, secret in oauth_secrets.items():
        check(secret not in blob, f"{name} absent from the public corpus")
        if "." in secret:
            check(secret.rsplit(".", 1)[-1] not in blob,
                  f"{name} signature fragment absent")
    # LAB_DATABASE_URL/DATABASE_URL fragments count too (host, user, password)
    for frag in ("db.sentinel.internal", "sentinel_user", "pw-SENTINEL-aB5xY1",
                 "db.lab-sentinel.internal", "lab_sentinel_user",
                 "pw-SENTINEL-mK6tE9"):
        check(frag not in blob, f"credential fragment '{frag}' absent")

    print("== exception hygiene (3b) ==")
    check("Traceback (most recent call last)" not in blob,
          "no tracebacks in any event/observation/feed payload")
    fetch_fails = [o for o in g.objects(type="observation")
                   if (o.data.get("metadata") or {}).get("lab") == "fetch_failure"]
    check(len(fetch_fails) >= 1, f"fetch-failure path exercised ({len(fetch_fails)})")
    budget = [o for o in g.objects(type="observation")
              if (o.data.get("metadata") or {}).get("lab") == "llm_budget"]
    check(len(budget) >= 1, f"budget-exhaustion path exercised ({len(budget)})")

    print(f"\ntest_public_safety: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
