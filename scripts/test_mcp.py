#!/usr/bin/env python3
"""MCP surface tests (ADR-016): initialize round-trip, read tools against a
fixture graph, send_chat round-trip with the source tag, pagination, auth
(tokenless → 401, wrong token → 403), cross-authority separation (the MCP
token never opens the inbox or pause; the operator token never opens /mcp),
and the rate limiter. In-process, no persistence, no key, no network.

Run:
    python scripts/test_mcp.py
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

MCP_TOKEN = "mcp-test-token-aaaa"
OPERATOR_TOKEN = "operator-test-token-bbbb"

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


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def canned_fetch(url: str, **_kw) -> dict:
    page = PAGES.get(url.rstrip("/"))
    if page is None:
        return {"url": url, "status": 404, "content": "", "error": "HTTPError: 404"}
    return {"url": url, "status": 200, "content": page}


def http(base: str, path: str, method: str = "POST", body: dict | None = None,
         token: str | None = None) -> tuple[int, dict]:
    r = urllib.request.Request(base + path, method=method)
    if token is not None:
        r.add_header("Authorization", f"Bearer {token}")
    r.add_header("Content-Type", "application/json")
    r.add_header("Accept", "application/json, text/event-stream")
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data=data, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


_RPC_ID = [0]


def rpc(base: str, method: str, params: dict | None = None,
        token: str | None = MCP_TOKEN) -> tuple[int, dict]:
    _RPC_ID[0] += 1
    msg = {"jsonrpc": "2.0", "id": _RPC_ID[0], "method": method,
           "params": params or {}}
    return http(base, "/mcp", "POST", msg, token=token)


def call_tool(base: str, name: str, args: dict | None = None,
              token: str | None = MCP_TOKEN) -> tuple[int, dict, dict | str]:
    """Returns (http_status, rpc_response, decoded tool payload or error text)."""
    s, resp = rpc(base, "tools/call", {"name": name, "arguments": args or {}},
                  token=token)
    result = resp.get("result") or {}
    text = (result.get("content") or [{}])[0].get("text", "")
    if result.get("isError"):
        return s, resp, text
    try:
        return s, resp, json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return s, resp, text


def main() -> int:
    import tempfile
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (LabMockProvider, LabProviderWrapper,
                              _lab_prompt_bodies, reset_llm_session)
    from lab_pack.settings import LabSettings
    from lab_pack.tools import approve_decision_fn
    from server import lab_server

    clear_lab_registry()
    reset_llm_session()
    rt = build_lab(
        llm_provider=LabProviderWrapper(LabMockProvider(), max_total=60,
                                        max_per_behavior=10,
                                        prompt_bodies=_lab_prompt_bodies()),
        lab_settings=LabSettings(crawl_page_cap=10, max_claims_per_page=3,
                                 drafts_dir=tempfile.mkdtemp(prefix="lab-mcp-")),
        fetch_handler=canned_fetch)
    rt.run_until_idle()
    g = rt.graph

    # Publish one post so get_post/list_posts have a subject.
    pub = next(d for d in g.objects(type="decision")
               if d.data.get("kind") == "publish" and d.data.get("status") == "pending")
    approve_decision_fn(g, pub.id, True, "test_mcp: publish one post")
    rt.run_until_idle()
    published = next(a for a in g.objects(type="artifact")
                     if a.data.get("status") == "published")
    slug = (published.data.get("metadata") or {}).get("slug")
    seed_branch = next(b for b in g.objects(type="branch"))

    lab_server._rt = rt
    lab_server._llm_info = {"mode": "mock", "provider": "mock", "model": None}
    httpd = HTTPServer(("127.0.0.1", 0), lab_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    saved_env = {k: os.environ.get(k) for k in ("LAB_MCP_TOKEN", "LAB_OPERATOR_TOKEN")}
    try:
        print("== auth: token unset / tokenless / wrong token ==")
        os.environ.pop("LAB_MCP_TOKEN", None)
        s, b = rpc(base, "initialize", token=MCP_TOKEN)
        check(s == 403 and "disabled" in b.get("error", ""),
              f"LAB_MCP_TOKEN unset → 403 mcp disabled ({s})")
        os.environ["LAB_MCP_TOKEN"] = MCP_TOKEN
        os.environ["LAB_OPERATOR_TOKEN"] = OPERATOR_TOKEN
        s, _ = rpc(base, "initialize", token=None)
        check(s == 401, f"tokenless POST /mcp → 401 ({s})")
        s, _ = rpc(base, "initialize", token="wrong-token")
        check(s == 403, f"wrong token → 403 ({s})")

        print("== cross-authority separation (ADR-016) ==")
        s, _ = http(base, "/lab/decision", "POST",
                    {"decision_id": pub.id, "approved": True}, token=MCP_TOKEN)
        check(s == 403, f"LAB_MCP_TOKEN on /lab/decision → 403 ({s})")
        s, _ = http(base, "/lab/pause", "POST", {}, token=MCP_TOKEN)
        check(s == 403, f"LAB_MCP_TOKEN on /lab/pause → 403 ({s})")
        s, _ = rpc(base, "initialize", token=OPERATOR_TOKEN)
        check(s == 403, f"LAB_OPERATOR_TOKEN on /mcp → 403 ({s})")

        print("== initialize round-trip ==")
        s, b = rpc(base, "initialize",
                   {"protocolVersion": "2025-06-18",
                    "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
        result = b.get("result") or {}
        check(s == 200 and result.get("protocolVersion") == "2025-06-18",
              f"initialize → 200 + protocol version ({result.get('protocolVersion')})")
        check(result.get("serverInfo", {}).get("name") == "activegraph-lab",
              "serverInfo present")
        check("tools" in (result.get("capabilities") or {}), "tools capability declared")
        # notifications/initialized is a notification → 202, no body
        s, _ = http(base, "/mcp", "POST",
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    token=MCP_TOKEN)
        check(s == 202, f"notifications/initialized → 202 ({s})")
        s, b = rpc(base, "ping")
        check(s == 200 and b.get("result") == {}, "ping → empty result")
        s, _ = http(base, "/mcp", "GET", None, token=MCP_TOKEN)
        check(s == 405, f"GET /mcp → 405 (no SSE channel) ({s})")

        print("== tools/list ==")
        s, b = rpc(base, "tools/list")
        tools = {t["name"] for t in (b.get("result") or {}).get("tools", [])}
        expected = {"get_status", "get_feed", "get_branch", "get_pending_decisions",
                    "get_post", "list_posts", "list_seams", "send_chat"}
        check(tools == expected, f"exactly the 8 ADR-016 tools ({sorted(tools)})")
        gate_tools = {"approve_decision", "reject_decision", "pause", "resume",
                      "promote_seam"}
        check(not (tools & gate_tools), "no gate authority exposed (excluded by design)")

        print("== read tools ==")
        s, _, status = call_tool(base, "get_status")
        check(s == 200 and status.get("event_count", 0) > 0
              and "pending_decisions" in status and "paused" in status,
              f"get_status projects healthz ({status.get('event_count')} events)")
        s, _, feed = call_tool(base, "get_feed", {"limit": 5})
        check(len(feed.get("entries", [])) == 5 and feed.get("next_cursor"),
              f"get_feed honors limit + cursor ({len(feed.get('entries', []))})")
        check(all(e.get("event_id") for e in feed["entries"]),
              "every feed entry carries its event id")
        first_ids = [e["event_id"] for e in feed["entries"]]
        s, _, page2 = call_tool(base, "get_feed",
                                {"limit": 5, "cursor": feed["next_cursor"]})
        page2_ids = [e["event_id"] for e in page2.get("entries", [])]
        check(page2_ids and not (set(first_ids) & set(page2_ids)),
              "cursor pages do not overlap")

        s, _, br = call_tool(base, "get_branch", {"branch_id": seed_branch.id})
        check(br.get("branch", {}).get("id") == seed_branch.id
              and len(br.get("entries", [])) > 0,
              f"get_branch returns the timeline ({len(br.get('entries', []))} entries)")
        check(len(br.get("evidence_ids", [])) >= 1,
              f"branch evidence ids attached ({len(br.get('evidence_ids', []))})")
        s, _, err = call_tool(base, "get_branch", {"branch_id": "branch#999"})
        check(isinstance(err, str) and "no such branch" in err,
              "get_branch on a bogus id is a tool error, not a 500")

        s, _, pend = call_tool(base, "get_pending_decisions")
        check(len(pend.get("pending", [])) >= 1
              and all("evidence" in d for d in pend["pending"]),
              f"pending decisions with evidence summaries ({len(pend.get('pending', []))})")

        s, _, posts = call_tool(base, "list_posts")
        check(any(p["slug"] == slug for p in posts.get("posts", [])),
              f"list_posts lists the published post ({slug})")
        check(all(p.get("published_event_id") for p in posts.get("posts", [])),
              "every post cites its artifact.published event id")
        s, _, post = call_tool(base, "get_post", {"slug": slug})
        check(post.get("slug") == slug and post.get("content")
              and "provenance" in post,
              "get_post returns body + provenance subgraph")
        s, _, err = call_tool(base, "get_post", {"slug": "nope"})
        check(isinstance(err, str) and "no published post" in err,
              "get_post on a bogus slug is a tool error")

        s, _, seams = call_tool(base, "list_seams")
        check("seams" in seams or "graph_code" in seams,
              f"list_seams projects the seams view ({list(seams)[:4]})")

        print("== send_chat round-trip (operator authority via MCP) ==")
        s, _, out = call_tool(base, "send_chat",
                              {"branch_id": seed_branch.id,
                               "message": "what is the state of this branch?"})
        check(s == 200 and out.get("reply") and "as of event" in out["reply"],
              "send_chat returns the stamped reply")
        check(out.get("event_horizon") is not None, "event horizon returned")
        check(len(out.get("created_event_ids", [])) >= 1,
              f"created event ids returned ({len(out.get('created_event_ids', []))})")
        msg = rt.graph.get_object(out["message_id"])
        check(msg is not None
              and (msg.data.get("metadata") or {}).get("source") == "operator_via_mcp",
              "comm_message tagged source=operator_via_mcp in the public log")
        check(msg is not None and msg.data.get("sender_ref") == "operator",
              "sender is the operator (no client-chosen identity)")
        s, _, br = call_tool(base, "get_branch", {"branch_id": seed_branch.id})
        check(any("(via MCP)" in e["sentence"] for e in br.get("entries", [])),
              "branch timeline interleaves the MCP chat, marked (via MCP)")
        s, _, err = call_tool(base, "send_chat",
                              {"branch_id": "branch#999", "message": "hi"})
        check(isinstance(err, str) and "no such branch" in err,
              "send_chat validates the branch exists")

        print("== rate limiter shared with the rest of the server ==")
        import time
        lab_server._mutation_times.clear()
        lab_server._mutation_times.extend([time.monotonic()] * 30)
        s, b = rpc(base, "tools/call",
                   {"name": "send_chat",
                    "arguments": {"branch_id": seed_branch.id, "message": "again"}})
        check(s == 429, f"31st mutation in a minute → 429 ({s})")
        lab_server._mutation_times.clear()
        s, _, feed = call_tool(base, "get_feed", {"limit": 1})
        check(s == 200 and feed.get("entries"),
              "read tools are not rate limited")

        print("== URL-token path (claude.ai connectors, ADR-016 amendment) ==")
        def rpc_url(path_token: str, method: str, params: dict | None = None):
            _RPC_ID[0] += 1
            return http(base, f"/mcp/{path_token}", "POST",
                        {"jsonrpc": "2.0", "id": _RPC_ID[0], "method": method,
                         "params": params or {}}, token=None)

        s, b = rpc_url(MCP_TOKEN, "initialize",
                       {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "test-url", "version": "0"}})
        check(s == 200
              and (b.get("result") or {}).get("protocolVersion") == "2025-06-18",
              "initialize via /mcp/<token>, no header → 200 round-trip")
        s, b = rpc_url(MCP_TOKEN, "tools/call",
                       {"name": "get_status", "arguments": {}})
        text = ((b.get("result") or {}).get("content") or [{}])[0].get("text", "{}")
        check(s == 200 and json.loads(text).get("event_count", 0) > 0,
              "read tool via URL token works")
        s, b = rpc_url(MCP_TOKEN, "tools/call",
                       {"name": "send_chat",
                        "arguments": {"branch_id": seed_branch.id,
                                      "message": "url-token check-in"}})
        result = b.get("result") or {}
        check(s == 200 and not result.get("isError"),
              "send_chat via URL token — identical authority to the header path")
        s, b = rpc_url("not-the-token", "initialize")
        check(s == 401, f"wrong token in path → 401 ({s})")
        check(MCP_TOKEN not in json.dumps(b) and "not-the-token" not in json.dumps(b),
              "401 body never echoes any token")
        os.environ.pop("LAB_MCP_TOKEN", None)
        s, _ = rpc_url(MCP_TOKEN, "initialize")
        check(s == 403, f"LAB_MCP_TOKEN unset → /mcp/<token> → 403 ({s})")
        os.environ["LAB_MCP_TOKEN"] = MCP_TOKEN
        s, _ = http(base, f"/lab/decision/{MCP_TOKEN}", "POST",
                    {"decision_id": pub.id, "approved": True})
        check(s == 404, f"URL token refused on /lab/decision (no such route, {s})")
        s, _ = http(base, f"/lab/pause/{MCP_TOKEN}", "POST", {})
        check(s == 404, f"URL token refused on /lab/pause (no such route, {s})")
        s, _ = http(base, f"/mcp/{MCP_TOKEN}", "GET")
        check(s == 405, f"GET /mcp/<token> → 405 like GET /mcp ({s})")

        print("== protocol edges ==")
        s, b = rpc(base, "no/such/method")
        check(s == 200 and (b.get("error") or {}).get("code") == -32601,
              "unknown method → JSON-RPC -32601")
        s, b = rpc(base, "tools/call", {"name": "no_such_tool", "arguments": {}})
        check((b.get("error") or {}).get("code") == -32602,
              "unknown tool → JSON-RPC -32602")
        r = urllib.request.Request(base + "/mcp", method="POST", data=b"not json{")
        r.add_header("Authorization", f"Bearer {MCP_TOKEN}")
        try:
            urllib.request.urlopen(r, timeout=10)
            check(False, "malformed body must not 200")
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b"{}")
            check(e.code == 400 and (body.get("error") or {}).get("code") == -32700,
                  f"malformed body → 400 + JSON-RPC parse error ({e.code})")
    finally:
        httpd.shutdown()
        lab_server._rt = None
        lab_server._mutation_times.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print(f"\ntest_mcp: {'PASS' if not FAILURES else 'FAIL'} ({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
