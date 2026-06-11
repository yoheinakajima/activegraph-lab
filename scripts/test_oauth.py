#!/usr/bin/env python3
"""OAuth 2.1 surface tests (ADR-017): RFC 8414/9728 metadata, stateless DCR
(deterministic client_id), the full DCR → authorize → PKCE → token →
tools/call round-trip with a fake client, wrong-token authorize, expired
code, tampered signatures, the refresh grant, cross-authority separation
(OAuth tokens never open the inbox or pause), the WWW-Authenticate
resource_metadata challenge, and that both legacy presentations
(bearer header, /mcp/<token>) still work. In-process, no persistence,
no key, no network.

Run:
    python scripts/test_oauth.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

FAILURES: list[str] = []

MCP_TOKEN = "mcp-test-token-oauth-aaaa"
OPERATOR_TOKEN = "operator-test-token-oauth-bbbb"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def req(base: str, path: str, method: str = "GET", data: bytes | None = None,
        headers: dict | None = None) -> tuple[int, dict, bytes]:
    """Returns (status, response_headers, body). Redirects NOT followed —
    the 302 Location from /authorize is the thing under test."""
    r = urllib.request.Request(base + path, method=method, data=data)
    for k, v in (headers or {}).items():
        r.add_header(k, v)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read() or b""


def jreq(base: str, path: str, method: str = "GET", body: dict | None = None,
         token: str | None = None) -> tuple[int, dict, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    s, h, raw = req(base, path, method, data, headers)
    try:
        return s, h, json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return s, h, {}


def form_post(base: str, path: str, fields: dict) -> tuple[int, dict, bytes]:
    return req(base, path, "POST", urllib.parse.urlencode(fields).encode(),
               {"Content-Type": "application/x-www-form-urlencoded"})


_RPC_ID = [100]


def rpc(base: str, method: str, params: dict | None = None,
        token: str | None = None) -> tuple[int, dict, dict]:
    _RPC_ID[0] += 1
    return jreq(base, "/mcp", "POST",
                {"jsonrpc": "2.0", "id": _RPC_ID[0], "method": method,
                 "params": params or {}}, token=token)


def call_tool(base: str, name: str, args: dict | None = None,
              token: str | None = None):
    s, _, resp = rpc(base, "tools/call",
                     {"name": name, "arguments": args or {}}, token=token)
    result = resp.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    if result.get("isError"):
        return s, resp, text
    try:
        return s, resp, json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return s, resp, text


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = b64u(secrets.token_bytes(32))
    return verifier, b64u(hashlib.sha256(verifier.encode()).digest())


def authorize(base: str, client_id: str, redirect_uri: str, challenge: str,
              token: str, state: str = "st-1") -> tuple[int, dict, bytes]:
    return form_post(base, "/authorize", {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": redirect_uri, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "token": token,
    })


def code_from_location(headers: dict) -> tuple[str, str]:
    loc = headers.get("Location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    return (q.get("code") or [""])[0], (q.get("state") or [""])[0]


def main() -> int:
    import tempfile
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import LabMockProvider, reset_llm_session
    from lab_pack.settings import LabSettings
    from server import lab_server, oauth

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

    saved_env = {k: os.environ.get(k) for k in ("LAB_MCP_TOKEN", "LAB_OPERATOR_TOKEN")}
    try:
        os.environ["LAB_MCP_TOKEN"] = MCP_TOKEN
        os.environ["LAB_OPERATOR_TOKEN"] = OPERATOR_TOKEN
        key = oauth.derive_key(MCP_TOKEN)

        print("== metadata (RFC 8414 / RFC 9728) ==")
        s, _, meta = jreq(base, "/.well-known/oauth-authorization-server")
        check(s == 200 and meta.get("authorization_endpoint", "").endswith("/authorize")
              and meta.get("token_endpoint", "").endswith("/token")
              and meta.get("registration_endpoint", "").endswith("/register"),
              "authorization-server metadata advertises all three endpoints")
        check(meta.get("code_challenge_methods_supported") == ["S256"],
              "PKCE S256 required")
        check(meta.get("token_endpoint_auth_methods_supported") == ["none"],
              "public clients only (no client_secret)")
        s, _, pr = jreq(base, "/.well-known/oauth-protected-resource")
        check(s == 200 and pr.get("resource", "").endswith("/mcp")
              and pr.get("authorization_servers") == [meta.get("issuer")],
              "protected-resource metadata points at /mcp + the issuer")
        s, _, m2 = jreq(base, "/.well-known/oauth-protected-resource/mcp")
        check(s == 200 and m2 == pr, "path-suffixed well-known variant served too")
        s, _, m3 = jreq(base, "/.well-known/oauth-authorization-server/mcp")
        check(s == 200 and m3 == meta, "AS metadata path-suffixed variant too")

        print("== DCR (RFC 7591, stateless/idempotent) ==")
        s, _, reg = jreq(base, "/register", "POST", {"redirect_uris": [CALLBACK]})
        client_id = reg.get("client_id", "")
        check(s == 201 and client_id.startswith("lab-"),
              f"register claude.ai callback → 201 + client_id")
        s, _, reg2 = jreq(base, "/register", "POST", {"redirect_uris": [CALLBACK]})
        check(reg2.get("client_id") == client_id,
              "re-registration is idempotent (deterministic client_id)")
        s, _, reg3 = jreq(base, "/register", "POST",
                          {"redirect_uris": ["https://claude.com/api/mcp/auth_callback"]})
        check(s == 201 and reg3.get("client_id") != client_id,
              "different redirect_uris → different client_id")
        s, _, regl = jreq(base, "/register", "POST",
                          {"redirect_uris": ["http://localhost:6274/oauth/callback"]})
        check(s == 201, "localhost redirect (e.g. MCP inspector) accepted")
        for evil in ("https://evil.example/cb", "http://claude.ai/cb",
                     "https://claude.ai.evil.example/cb", "https://notclaude.ai/cb"):
            s, _, err = jreq(base, "/register", "POST", {"redirect_uris": [evil]})
            check(s == 400 and err.get("error") == "invalid_redirect_uri",
                  f"reject {evil}")
        s, _, err = jreq(base, "/register", "POST", {})
        check(s == 400, "missing redirect_uris → 400")

        print("== GET /authorize (the operator's page) ==")
        verifier, challenge = pkce_pair()
        q = urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id,
            "redirect_uri": CALLBACK, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": "st-1"})
        s, _, page = req(base, f"/authorize?{q}")
        check(s == 200 and b'type="password"' in page and challenge.encode() in page,
              "authorize page renders the password form with bound params")
        check(MCP_TOKEN.encode() not in page, "page never contains the token")
        s, _, _b = req(base, f"/authorize?{q.replace('S256', 'plain')}")
        check(s == 400, "code_challenge_method=plain → 400 (S256 only)")
        q_bad = urllib.parse.urlencode({"response_type": "code",
                                        "client_id": client_id,
                                        "redirect_uri": "https://evil.example/cb",
                                        "code_challenge": challenge})
        s, _, body = req(base, f"/authorize?{q_bad}")
        check(s == 400 and b"evil.example" not in body,
              "bad redirect_uri → 400, value not echoed")
        s, _, _b = req(base, "/authorize?response_type=code&client_id=x&redirect_uri="
                       + urllib.parse.quote(CALLBACK))
        check(s == 400, "missing code_challenge → 400")

        print("== wrong-token authorize: 401, no oracle, no redirect ==")
        s, h, body = authorize(base, client_id, CALLBACK, challenge,
                               token="not-the-token")
        check(s == 401 and "Location" not in h, "wrong token → 401, no redirect")
        check(MCP_TOKEN.encode() not in body and b"not-the-token" not in body
              and b"expir" not in body.lower() and b"invalid" not in body.lower(),
              "401 body is fixed — echoes nothing, explains nothing")
        s, h, _b = authorize(base, client_id, CALLBACK, challenge, token="")
        check(s == 401, "empty token → 401")

        print("== full round-trip: authorize → code → PKCE → token → tools/call ==")
        s, h, _b = authorize(base, client_id, CALLBACK, challenge, MCP_TOKEN,
                             state="xyz-state")
        code, state = code_from_location(h)
        check(s == 302 and h.get("Location", "").startswith(CALLBACK + "?")
              and code, "correct token → 302 to redirect_uri with a code")
        check(state == "xyz-state", "state round-trips")
        s, h, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": CALLBACK})
        tok = json.loads(raw)
        access, refresh = tok.get("access_token", ""), tok.get("refresh_token", "")
        check(s == 200 and access and refresh
              and tok.get("token_type") == "Bearer"
              and tok.get("expires_in") == 24 * 3600,
              "code + verifier → access (24h) + refresh tokens")
        check(h.get("Cache-Control") == "no-store", "token response is no-store")
        check(access != MCP_TOKEN and refresh != access,
              "minted tokens are not the root secret")
        s, _, st = call_tool(base, "get_status", token=access)
        check(s == 200 and st.get("event_count", 0) > 0,
              "tools/call get_status with the OAuth bearer works")
        s, _, out = call_tool(base, "send_chat",
                              {"branch_id": branch.id,
                               "message": "oauth round-trip check-in"},
                              token=access)
        check(s == 200 and isinstance(out, dict)
              and out.get("status") in ("ok", "reply_pending"),
              "send_chat with the OAuth bearer — identical authority (ADR-016)")
        msg = rt.graph.get_object(out.get("message_id"))
        check(msg is not None
              and (msg.data.get("metadata") or {}).get("source") == "operator_via_mcp",
              "OAuth-borne chat still tagged source=operator_via_mcp")
        s, _, resp = rpc(base, "tools/list", token=access)
        tools = {t["name"] for t in (resp.get("result") or {}).get("tools", [])}
        check("send_chat" in tools and not tools & {"approve_decision", "pause",
                                                    "resume", "promote_seam"},
              "OAuth bearer sees the same 8 tools — no gate authority")

        print("== grant edges: replayed-ish, wrong verifier, binding, expiry ==")
        s, _, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": code,
            "code_verifier": "wrong-verifier-wrong-verifier-wrong-wrong",
            "client_id": client_id, "redirect_uri": CALLBACK})
        check(s == 400 and json.loads(raw).get("error") == "invalid_grant",
              "wrong PKCE verifier → invalid_grant")
        s, _, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "client_id": "lab-someoneelse",
            "redirect_uri": CALLBACK})
        check(s == 400, "client_id not bound in the code → invalid_grant")
        s, _, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": code,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": "http://localhost:6274/oauth/callback"})
        check(s == 400, "redirect_uri not bound in the code → invalid_grant")
        expired = oauth._sign(key, {"typ": "code", "cid": client_id,
                                    "ruri": CALLBACK, "chal": challenge,
                                    "exp": int(time.time()) - 5})
        s, _, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": expired,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": CALLBACK})
        check(s == 400 and json.loads(raw).get("error") == "invalid_grant",
              "expired code (60s TTL) → invalid_grant")
        tampered_code = code[:-4] + ("AAAA" if code[-4:] != "AAAA" else "BBBB")
        s, _, raw = form_post(base, "/token", {
            "grant_type": "authorization_code", "code": tampered_code,
            "code_verifier": verifier, "client_id": client_id,
            "redirect_uri": CALLBACK})
        check(s == 400, "tampered code signature → invalid_grant")
        s, _, raw = form_post(base, "/token", {"grant_type": "password",
                                               "username": "x", "password": "y"})
        check(s == 400 and json.loads(raw).get("error") == "unsupported_grant_type",
              "non-2.1 grant → unsupported_grant_type")

        print("== tampered/expired access tokens on /mcp ==")
        tampered = access[:-4] + ("AAAA" if access[-4:] != "AAAA" else "BBBB")
        s, h, body = rpc(base, "initialize", token=tampered)
        check(s == 401, f"tampered access-token signature → 401 ({s})")
        check("resource_metadata" in h.get("WWW-Authenticate", ""),
              "401 challenge carries resource_metadata (MCP auth spec)")
        check(access not in json.dumps(body) and tampered not in json.dumps(body),
              "401 body never echoes the token")
        expired_at = oauth._sign(key, {"typ": "access", "cid": client_id,
                                       "scope": "mcp",
                                       "exp": int(time.time()) - 5})
        s, _h, _b = rpc(base, "initialize", token=expired_at)
        check(s == 401, f"expired access token → 401 (clients refresh) ({s})")
        s, _h, _b = rpc(base, "initialize", token=refresh)
        check(s == 401, "refresh token is NOT an access token on /mcp → 401")
        forged = oauth._sign(oauth.derive_key("some-other-key"),
                             {"typ": "access", "cid": client_id, "scope": "mcp",
                              "exp": int(time.time()) + 3600})
        s, _h, _b = rpc(base, "initialize", token=forged)
        check(s == 401, "token signed with a different key → 401")

        print("== refresh grant ==")
        s, _, raw = form_post(base, "/token", {"grant_type": "refresh_token",
                                               "refresh_token": refresh})
        tok2 = json.loads(raw)
        access2 = tok2.get("access_token", "")
        check(s == 200 and access2 and tok2.get("refresh_token"),
              "refresh grant → new access + refresh pair")
        s, _, st = call_tool(base, "get_status", token=access2)
        check(s == 200 and st.get("event_count", 0) > 0,
              "refreshed access token works on /mcp")
        s, _, raw = form_post(base, "/token", {"grant_type": "refresh_token",
                                               "refresh_token": access})
        check(s == 400, "access token is NOT a refresh token → invalid_grant")

        print("== cross-authority (ADR-016 unchanged): OAuth never opens the gate ==")
        s, _, _b = jreq(base, "/lab/decision", "POST",
                        {"decision_id": "decision#1", "approved": True}, token=access)
        check(s == 403, f"OAuth access token on /lab/decision → 403 ({s})")
        s, _, _b = jreq(base, "/lab/pause", "POST", {}, token=access)
        check(s == 403, f"OAuth access token on /lab/pause → 403 ({s})")
        s, _, _b = rpc(base, "initialize", token=OPERATOR_TOKEN)
        check(s == 403, "operator token still refused on /mcp")

        print("== legacy presentations remain (ADR-016) ==")
        s, _, resp = rpc(base, "initialize", token=MCP_TOKEN)
        check(s == 200 and (resp.get("result") or {}).get("protocolVersion"),
              "legacy LAB_MCP_TOKEN bearer header still works")
        _RPC_ID[0] += 1
        s, _, resp = jreq(base, f"/mcp/{MCP_TOKEN}", "POST",
                          {"jsonrpc": "2.0", "id": _RPC_ID[0],
                           "method": "initialize", "params": {}})
        check(s == 200 and (resp.get("result") or {}).get("protocolVersion"),
              "legacy /mcp/<token> path still works")
        s, h, _b = rpc(base, "initialize", token=None)
        check(s == 401 and "resource_metadata" in h.get("WWW-Authenticate", ""),
              "tokenless /mcp → 401 + resource_metadata challenge (discovery)")
        s, _h, _b = rpc(base, "initialize", token="wrong-token")
        check(s == 403, "non-OAuth-shaped wrong bearer keeps the legacy 403")

        print("== LAB_MCP_TOKEN unset disables the whole OAuth surface ==")
        os.environ.pop("LAB_MCP_TOKEN", None)
        s, _, _b = jreq(base, "/register", "POST", {"redirect_uris": [CALLBACK]})
        check(s == 403, f"unset → /register 403 ({s})")
        s, _, raw = form_post(base, "/token", {"grant_type": "refresh_token",
                                               "refresh_token": refresh})
        check(s == 403, f"unset → /token 403 ({s})")
        s, _, _b = authorize(base, client_id, CALLBACK, challenge, MCP_TOKEN)
        check(s == 403, f"unset → POST /authorize 403 ({s})")
        s, _, _b = req(base, f"/authorize?{q}")
        check(s == 403, f"unset → GET /authorize 403 ({s})")
        os.environ["LAB_MCP_TOKEN"] = MCP_TOKEN
        s, _, st = call_tool(base, "get_status", token=access)
        check(s == 200, "token restored → minted access tokens valid again "
                        "(rotation = revocation)")
    finally:
        httpd.shutdown()
        lab_server._rt = None
        lab_server._mutation_times.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\ntest_oauth: {'PASS' if not FAILURES else 'FAIL'} ({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
