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
                    "get_post", "list_posts", "list_seams", "get_errors",
                    "get_log", "get_entity",
                    "send_chat", "set_budget", "pause_lab", "resume_lab"}
        check(tools == expected,
              f"exactly the ADR-016 tools + get_errors (ADR-023) + the "
              f"ADR-021 expansion ({sorted(tools)})")
        check("get_errors" in tools, "get_errors present in the READ tier (ADR-023)")
        gate_tools = {"approve_decision", "reject_decision", "promote_seam",
                      "approve", "reject"}
        check(not (tools & gate_tools),
              "no gate authority exposed — approve/reject and seam promotion "
              "stay human-only (ADR-021)")

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

        s, _, errs = call_tool(base, "get_errors")
        check(s == 200 and "errors" in errs and "note" in errs,
              "get_errors projects the diagnostics ring buffer (ADR-023)")

        print("== get_log / get_entity parity with the HTTP projections (ADR-021) ==")
        def http_get(path):
            r = urllib.request.Request(base + path)
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read())
        s, _, log = call_tool(base, "get_log", {"limit": 10})
        check(s == 200 and len(log.get("rows", [])) == 10
              and log.get("total", 0) > 10,
              f"get_log returns one-line rows ({len(log.get('rows', []))}/"
              f"{log.get('total')})")
        check(log == http_get("/lab/log?limit=10"),
              "get_log is byte-identical to GET /lab/log")
        s, _, older = call_tool(base, "get_log",
                                {"limit": 10, "before": log["oldest_rendered"]})
        ids_a = {r["event_id"] for r in log["rows"]}
        ids_b = {r["event_id"] for r in older.get("rows", [])}
        check(ids_b and not (ids_a & ids_b), "get_log cursor pages do not overlap")
        check(all((r.get("summary") or "").strip() for r in log["rows"]),
              "no log row renders blank")

        s, _, ent = call_tool(base, "get_entity", {"id": seed_branch.id})
        check(s == 200 and ent.get("kind") == "object"
              and (ent.get("relations_out") or ent.get("relations_in")),
              "get_entity on an object id returns fields + relations")
        from urllib.parse import quote
        check(ent == http_get(f"/lab/entity?id={quote(str(seed_branch.id), safe='')}"),
              "get_entity is byte-identical to GET /lab/entity")
        evt_id = log["rows"][0]["event_id"]
        s, _, ev = call_tool(base, "get_entity", {"id": evt_id})
        check(ev.get("kind") == "event" and ev.get("prev_id")
              and "refs" in ev,
              "get_entity on an event id returns payload + prev/next + refs")
        s, _, err = call_tool(base, "get_entity", {"id": "thing#9999"})
        check(isinstance(err, str) and "no such entity" in err,
              "get_entity on a bogus id is a tool error, not a 500")

        print("== operator-control tier (ADR-021): set_budget clamps to the ceiling ==")
        from lab_pack.kernel import ABSOLUTE_DAILY_COST_CEILING_USD as CEIL
        s, _, out = call_tool(base, "set_budget", {"amount_usd": 500})
        check(s == 200 and out.get("new_usd") == CEIL and out.get("clamped") is True,
              f"set_budget 500 clamps to the kernel ceiling ({out.get('new_usd')})")
        s, _, st = call_tool(base, "get_status")
        check(st.get("llm_cost_cap") == CEIL,
              f"status reports the clamped cap ({st.get('llm_cost_cap')})")
        s, _, out = call_tool(base, "set_budget",
                              {"amount_usd": 12.5, "today_only": True})
        check(out.get("new_usd") == 12.5 and "today_only" in out.get("scope", ""),
              f"today-only cap under the ceiling passes through ({out})")
        s, _, st = call_tool(base, "get_status")
        check(st.get("llm_cost_cap") == 12.5,
              f"today-only cap in force ({st.get('llm_cost_cap')})")
        bevts = [e for e in rt.graph.events if str(e.type) == "lab.budget_set"]
        check(len(bevts) == 2
              and bevts[-1].payload.get("old_usd") == CEIL
              and bevts[-1].payload.get("new_usd") == 12.5
              and bevts[-1].payload.get("today_only") is True
              and bevts[-1].payload.get("by") == "operator_via_mcp",
              "public control events record old → new and scope")
        # today_only resets at UTC midnight: backdate the marker, resync (the
        # log is the persistence) — the persistent cap resumes.
        bevts[-1].payload["date"] = "2000-01-01"
        from lab_pack.llm import sync_daily_budget
        sync_daily_budget(rt)
        s, _, st = call_tool(base, "get_status")
        check(st.get("llm_cost_cap") == CEIL,
              f"expired today-only cap → the persistent cap resumes "
              f"({st.get('llm_cost_cap')})")
        s, _, err = call_tool(base, "set_budget", {"amount_usd": -3})
        check(isinstance(err, str) and "positive" in err,
              "set_budget validates the amount")

        print("== operator-control tier (ADR-021): pause/resume via MCP ==")
        from lab_pack.llm import reset_llm_run_counters
        s, _, out = call_tool(base, "pause_lab")
        check(s == 200 and out.get("paused") is True and out.get("changed") is True,
              "pause_lab pauses (public lab.paused event)")
        s, _, st = call_tool(base, "get_status")
        check(st.get("paused") is True, "status shows paused")
        proposed_before = len([b for b in g.objects(type="branch")
                               if b.data.get("status") == "proposed"])
        reset_llm_run_counters()
        g.add_object("observation", {
            "text": "Claim while paused: replay rebuilds state deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": None}})
        rt.run_until_idle()
        proposed_paused = len([b for b in g.objects(type="branch")
                               if b.data.get("status") == "proposed"])
        check(proposed_paused == proposed_before,
              "behaviors idle while paused via MCP")
        s, _, out = call_tool(base, "resume_lab")
        check(out.get("paused") is False and out.get("changed") is True,
              "resume_lab resumes and drains")
        reset_llm_run_counters()
        g.add_object("observation", {
            "text": "Claim after resume: replay rebuilds state deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": None}})
        rt.run_until_idle()
        proposed_resumed = len([b for b in g.objects(type="branch")
                                if b.data.get("status") == "proposed"])
        check(proposed_resumed == proposed_paused + 1,
              f"behaviors fire again after resume "
              f"({proposed_resumed - proposed_paused} new proposal)")
        check(any(str(e.type) == "lab.paused" for e in g.events)
              and any(str(e.type) == "lab.resumed" for e in g.events),
              "pause/resume control events are in the public log")

        print("== send_chat round-trip (operator authority via MCP) ==")
        s, _, out = call_tool(base, "send_chat",
                              {"branch_id": seed_branch.id,
                               "message": "what is the state of this branch?"})
        check(s == 200 and out.get("status") == "ok"
              and out.get("reply") and "as of event" in out["reply"],
              "send_chat returns status=ok with the stamped reply")
        check(out.get("event_horizon") is not None, "event horizon returned")
        check(len(out.get("message_event_ids", [])) >= 1,
              f"message event ids returned ({len(out.get('message_event_ids', []))})")
        check(len(out.get("created_event_ids", [])) >= 1,
              f"created event ids returned ({len(out.get('created_event_ids', []))})")
        msg = rt.graph.get_object(out["message_id"])
        check(msg is not None
              and (msg.data.get("metadata") or {}).get("source") == "operator_via_mcp",
              "comm_message tagged source=operator_via_mcp in the public log")
        check(msg is not None and msg.data.get("sender_ref") == "operator",
              "sender is the operator (no client-chosen identity)")
        # ADR-016 invariant: the source tag is provenance, never an answer
        # predicate — the via-MCP message drew exactly one lab.answer reply.
        mcp_cands = [c for c in rt.graph.objects(type="comm_response_candidate")
                     if str(c.data.get("message_id")) == str(out["message_id"])
                     and c.data.get("created_by_behavior") == "lab.answer"]
        check(len(mcp_cands) == 1,
              f"operator_via_mcp message answered exactly once ({len(mcp_cands)})")
        s, _, br = call_tool(base, "get_branch", {"branch_id": seed_branch.id})
        check(any("(via MCP)" in e["sentence"] for e in br.get("entries", [])),
              "branch timeline interleaves the MCP chat, marked (via MCP)")
        s, _, err = call_tool(base, "send_chat",
                              {"branch_id": "branch#999", "message": "hi"})
        check(isinstance(err, str) and "no such branch" in err,
              "send_chat validates the branch exists")

        print("== send_chat reply timeout → structured partial, not a generic error ==")
        # Simulate a stuck worker for the reply phase only: the bounded wait
        # is the one call that passes an explicit timeout, so key on that.
        real_row = lab_server._run_on_worker
        seen_waits: list = []
        def stuck_reply(fn, timeout=180):
            if timeout != 180:
                seen_waits.append(timeout)
                raise TimeoutError("simulated: runtime worker did not finish in time")
            return real_row(fn, timeout)
        lab_server._run_on_worker = stuck_reply
        try:
            s, _, out = call_tool(base, "send_chat",
                                  {"branch_id": seed_branch.id,
                                   "message": "does the timeout path stay structured?"})
        finally:
            lab_server._run_on_worker = real_row
        check(s == 200 and isinstance(out, dict)
              and out.get("status") == "reply_pending",
              f"timed-out reply → structured partial success ({out.get('status') if isinstance(out, dict) else out})")
        check(seen_waits == [15],
              f"reply wait defaults to 15s — under claude.ai's tool timeout "
              f"({seen_waits})")
        check(len(out.get("message_event_ids", [])) >= 1,
              "partial carries the committed message event ids")
        check("get_branch" in (out.get("detail") or ""),
              "partial tells the caller to poll get_branch")
        pending_msg_id = out.get("message_id")
        check(rt.graph.get_object(pending_msg_id) is not None,
              "the message itself DID land in the log")
        # Drain the runtime (the worker finishing late) — the reply arrives,
        # exactly once: re-triggering must not double-fire the answer.
        rt.run_until_idle()
        late = [c for c in rt.graph.objects(type="comm_response_candidate")
                if str(c.data.get("message_id")) == str(pending_msg_id)
                and c.data.get("created_by_behavior") == "lab.answer"]
        check(len(late) == 1,
              f"late reply lands exactly once after the drain ({len(late)})")
        s, _, br = call_tool(base, "get_branch", {"branch_id": seed_branch.id})
        check(any("timeout path" in e["sentence"] for e in br.get("entries", [])),
              "get_branch shows the pending message (the advertised poll path)")

        print("== reply wait is a seam setting; slow reply → reply_pending within the bound ==")
        import time
        from lab_pack.seams import propose_seam_fn
        propose_seam_fn(rt.graph, "setting.mcp_reply_wait_seconds", "1",
                        "test_mcp: tighten the MCP reply wait")
        rt.run_until_idle()
        dw = next(d for d in rt.graph.objects(type="decision")
                  if d.data.get("kind") == "self_modify"
                  and d.data.get("status") == "pending"
                  and (d.data.get("metadata") or {}).get("seam_name")
                  == "setting.mcp_reply_wait_seconds")
        approve_decision_fn(rt.graph, dw.id, True, "test_mcp: approve reply wait seam")
        rt.run_until_idle()
        # A reply phase slower than the bound: test mode runs jobs inline and
        # ignores the timeout, so enforce it for real here — and substitute a
        # reply collector that cannot land within the 1s seam bound. The
        # sleeper never touches the runtime, so the side thread is safe.
        real_collect = lab_server._chat_collect_reply
        def slow_collect(rt_, message_id):
            time.sleep(6)
            return None
        def bounded_row(fn, timeout=180):
            if timeout == 180:
                return real_row(fn, timeout)
            seen_waits.append(timeout)
            box: dict = {}
            def run():
                try:
                    box["result"] = fn(lab_server._rt)
                except BaseException as exc:
                    box["error"] = exc
            t = threading.Thread(target=run, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                raise TimeoutError("runtime worker did not finish in time")
            if "error" in box:
                raise box["error"]
            return box.get("result")
        seen_waits.clear()
        lab_server._chat_collect_reply = slow_collect
        lab_server._run_on_worker = bounded_row
        t0 = time.monotonic()
        try:
            s, _, out = call_tool(base, "send_chat",
                                  {"branch_id": seed_branch.id,
                                   "message": "does a slow reply stay within the bound?"})
        finally:
            lab_server._chat_collect_reply = real_collect
            lab_server._run_on_worker = real_row
        elapsed = time.monotonic() - t0
        check(seen_waits == [1],
              f"approved seam overrides the reply wait (15 → 1) ({seen_waits})")
        check(s == 200 and isinstance(out, dict)
              and out.get("status") == "reply_pending" and elapsed < 5,
              f"slow reply → reply_pending within the bound ({elapsed:.1f}s)")
        check(rt.graph.get_object(out.get("message_id")) is not None
              if isinstance(out, dict) else False,
              "the slow-path message still landed in the log")
        rt.run_until_idle()  # drain the pending answer before later sections

        print("== rate limiter shared with the rest of the server ==")
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
