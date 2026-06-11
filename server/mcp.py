"""MCP surface (ADR-016) — streamable HTTP, implemented minimally by hand.

JSON-RPC 2.0 over POST /mcp, single-JSON responses (a mode the streamable
HTTP spec permits; no SSE stream, no sessions). The official Python MCP SDK
was evaluated and declined — its streamable-HTTP transport is ASGI-only and
the lab server is stdlib http.server (ADR-016 records the reasoning).

No new state: every READ tool is a projection of the event log; send_chat
goes through the same code path as POST /chat. Auth (LAB_MCP_TOKEN) and the
rate limiter live in server/lab_server.py (kernel, ADR-012); this module
only sees already-authorized messages.

Tool tiers (ADR-016; get_errors added by ADR-023):
  READ:     get_status, get_feed, get_branch, get_pending_decisions,
            get_post, list_posts, list_seams, get_errors
  OPERATOR: send_chat (tagged source=operator_via_mcp in the public log)
  EXCLUDED BY DESIGN: decision approval, pause/resume, seam promotion —
            the inbox is the one place only the human operator exists.
"""

from __future__ import annotations

import json
from typing import Any, Optional

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "activegraph-lab", "version": "0.1.0"}
INSTRUCTIONS = (
    "Read-and-talk surface for the activegraph-lab research agent. READ tools "
    "project the public event log (cite event ids like evt_42 and branch ids "
    "back to the operator). send_chat posts to a branch thread AS the operator "
    "— it lands in the public log. Approving decisions, pausing, and seam "
    "promotion are deliberately not available here (ADR-016)."
)

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 200

TOOLS: list[dict] = [
    {
        "name": "get_status",
        "description": "Lab health: backend, event count, pending decisions, "
                       "pause state, LLM calls/cost against caps.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_feed",
        "description": "The notebook feed, newest first. Paginated: pass the "
                       "returned next_cursor to get older entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cursor": {"type": "string",
                           "description": "Return entries older than this event id."},
                "limit": {"type": "integer", "description": "Max entries (default 30, max 200)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_branch",
        "description": "Full timeline for one branch (chats interleaved), plus "
                       "its evidence and pending decisions. Paginated like get_feed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string", "description": "e.g. branch#3"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["branch_id"],
        },
    },
    {
        "name": "get_pending_decisions",
        "description": "The inbox: pending decisions with evidence summaries. "
                       "Resolution happens only in the human operator's inbox, "
                       "never through MCP (ADR-016).",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_post",
        "description": "One published blog post: markdown body + provenance "
                       "subgraph (branch, evidence, chat, publish decision).",
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string"}},
            "required": ["slug"],
        },
    },
    {
        "name": "list_posts",
        "description": "Published blog posts, newest first.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_seams",
        "description": "Every self-modification surface: active seam versions, "
                       "their source (file|graph), and graph-code draft states.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_errors",
        "description": "Diagnostics: the last unhandled/degraded exceptions "
                       "(ts, class, sanitized message, request kind, related "
                       "event ids). Volatile in-process ring buffer (ADR-023) "
                       "— lost on restart, never authoritative.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "Max entries, newest first (default all)."},
            },
            "required": [],
        },
    },
    {
        "name": "send_chat",
        "description": "Post a message into a branch's thread WITH OPERATOR "
                       "AUTHORITY. The message is public and tagged "
                       "source=operator_via_mcp. Returns status=ok with the "
                       "lab's reply, its event-horizon stamp, and the event "
                       "ids created — or status=reply_pending with the "
                       "message event ids when the message landed but the "
                       "reply missed the bounded wait (default 15s; poll "
                       "get_branch).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["branch_id", "message"],
        },
    },
]


# ---------------------------------------------------------------- projections


def _compact_entry(e: dict) -> dict:
    out = {"event_id": e["event_id"], "ts": e["timestamp"],
           "branch_id": e["branch_id"], "kind": e["kind"],
           "sentence": e["sentence"]}
    if e.get("post_url"):
        out["post_url"] = e["post_url"]
    if e.get("artifact"):
        out["artifact"] = e["artifact"]
    return out


def _paginate(entries: list[dict], cursor: Optional[str], limit: Any) -> dict:
    """Shared cursor pagination: entries are chronological; output is
    newest-first; next_cursor is the oldest event id of this page."""
    from server.lab_server import _event_index
    try:
        limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    if cursor:
        # Compare event-id ordinals, not enumeration indexes (ids are 1-based).
        cut = _event_index(cursor)
        entries = [e for e in entries if _event_index(e["event_id"]) < cut]
    page = entries[-limit:]
    return {
        "entries": [_compact_entry(e) for e in reversed(page)],
        "next_cursor": page[0]["event_id"] if len(entries) > len(page) else None,
    }


def _tool_get_status(rt, args: dict) -> dict:
    from lab_pack import storage
    from server.lab_server import _llm_info, _operator_status
    g = rt.graph
    pending = sum(1 for d in g.objects(type="decision")
                  if d.data.get("status") == "pending")
    return {
        "status": "ok",
        "backend": storage.backend(),
        "event_count": len(g.events),
        "last_event_id": str(g.events[-1].id) if g.events else None,
        "last_event_ts": str(g.events[-1].timestamp) if g.events else None,
        "pending_decisions": pending,
        "llm": _llm_info,
        **_operator_status(),
    }


def _tool_get_feed(rt, args: dict) -> dict:
    from server.lab_server import _build_entries
    g = rt.graph
    out = _paginate(_build_entries(g), args.get("cursor"), args.get("limit"))
    out["as_of_event"] = str(g.events[-1].id) if g.events else None
    return out


def _tool_get_branch(rt, args: dict) -> dict:
    from lab_pack.compat import decode_relation
    from server.lab_server import _build_entries, _pending_decisions
    g = rt.graph
    branch_id = args.get("branch_id") or ""
    b = g.get_object(branch_id)
    if b is None or str(b.type) != "branch":
        raise ToolError(f"no such branch: {branch_id}")
    entries = [e for e in _build_entries(g) if e["branch_id"] == branch_id]
    out = _paginate(entries, args.get("cursor"), args.get("limit"))
    evidence_ids = []
    for r in g.relations():
        rel_type, src, tgt = decode_relation(r)
        if rel_type == "supported_by" and src == branch_id:
            evidence_ids.append(tgt)
    out["branch"] = {"id": str(b.id), "title": b.data.get("title"),
                     "status": b.data.get("status"),
                     "intent": b.data.get("intent"),
                     "authority": b.data.get("authority")}
    out["evidence_ids"] = evidence_ids
    out["pending_decisions"] = [d for d in _pending_decisions(g)
                                if d.get("subject_ref") == branch_id
                                or d.get("branch_id") == branch_id]
    return out


def _tool_get_pending_decisions(rt, args: dict) -> dict:
    from server.lab_server import _pending_decisions
    return {"pending": _pending_decisions(rt.graph)}


def _published_event_id(g, artifact_id: str) -> Optional[str]:
    for e in g.events:
        if str(e.type) == "artifact.published" \
                and e.payload.get("artifact_id") == artifact_id:
            return str(e.id)
    return None


def _post_summary(g, a) -> dict:
    meta = a.data.get("metadata") or {}
    return {
        "slug": meta.get("slug"),
        "title": a.data.get("title"),
        "post_kind": meta.get("post_kind"),
        "published_at": meta.get("published_at"),
        "artifact_id": str(a.id),
        "branch_id": meta.get("lab_branch_id"),
        "published_event_id": _published_event_id(g, str(a.id)),
    }


def _tool_get_post(rt, args: dict) -> dict:
    from server import blog
    g = rt.graph
    slug = args.get("slug") or ""
    a = blog.post_by_slug(g, slug)
    if a is None:
        raise ToolError(f"no published post with slug: {slug}")
    out = _post_summary(g, a)
    out["content"] = a.data.get("content") or ""
    out["provenance"] = blog.provenance(g, a)
    return out


def _tool_list_posts(rt, args: dict) -> dict:
    from server import blog
    g = rt.graph
    return {"posts": [_post_summary(g, a) for a in blog.published_posts(g)]}


def _tool_list_seams(rt, args: dict) -> dict:
    from lab_pack import graph_code, seams
    out = seams.seam_status(rt.graph)
    out.update(graph_code.status(rt.graph))
    return out


def _tool_get_errors(rt, args: dict) -> dict:
    from server.lab_server import _ERRORS, _ERRORS_MAX
    entries = list(reversed(_ERRORS))
    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit > 0:
        entries = entries[:limit]
    return {"errors": entries, "max": _ERRORS_MAX,
            "note": ("volatile in-process diagnostics (ADR-023): lost on "
                     "restart, never authoritative — the event log is")}


_READ_TOOLS = {
    "get_status": _tool_get_status,
    "get_feed": _tool_get_feed,
    "get_branch": _tool_get_branch,
    "get_pending_decisions": _tool_get_pending_decisions,
    "get_post": _tool_get_post,
    "list_posts": _tool_list_posts,
    "list_seams": _tool_list_seams,
    "get_errors": _tool_get_errors,
}


class ToolError(Exception):
    """Tool-level failure: rendered as an isError tool result, not a 500."""


# ---------------------------------------------------------------- dispatch


def _rpc_result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _tool_result(msg_id: Any, payload: dict) -> dict:
    text = json.dumps(payload, default=str, separators=(",", ":"))
    return _rpc_result(msg_id, {"content": [{"type": "text", "text": text}],
                                "isError": False})


def _tool_failure(msg_id: Any, message: str) -> dict:
    return _rpc_result(msg_id, {"content": [{"type": "text", "text": message}],
                                "isError": True})


# Bounded wait for the reply phase. MCP clients enforce their own tool
# timeouts (claude.ai errors out well under the answer behavior's 60s LLM
# cap), so the wait must come in under theirs: past the bound the message is
# still committed and the reply, if it lands, is visible via get_branch — so
# the tool reports partial success instead of the client seeing a transport
# error. The value is setting.mcp_reply_wait_seconds, seam-whitelisted
# (lab_pack/kernel.py), default 15.
def _reply_wait_seconds(graph) -> int:
    from lab_pack.seams import effective_setting
    from lab_pack.settings import LabSettings
    return max(1, int(effective_setting(graph, LabSettings(),
                                        "mcp_reply_wait_seconds")))


def _send_chat(msg_id: Any, args: dict, *, get_rt, lock, run_on_worker) -> dict:
    from lab_pack.llm import reset_llm_run_counters
    from server.lab_server import (_chat_collect_reply_safe,
                                   _chat_post_message, _record_error,
                                   _submit_to_worker)
    branch_id = (args.get("branch_id") or "").strip()
    message = (args.get("message") or "").strip()
    if not branch_id or not message:
        return _tool_failure(msg_id, "branch_id and message are required")
    rt = get_rt()
    with lock:
        b = rt.graph.get_object(branch_id)
        status = b.data.get("status") if b is not None and str(b.type) == "branch" else None
        reply_wait = _reply_wait_seconds(rt.graph)
    if status is None:
        return _tool_failure(msg_id, f"no such branch: {branch_id}")
    if status == "archived":
        return _tool_failure(msg_id, f"branch {branch_id} is archived (not chat-able)")
    reset_llm_run_counters()
    try:
        posted = run_on_worker(
            lambda rt: _chat_post_message(rt, branch_id, message,
                                          source="operator_via_mcp"))
    except Exception as exc:
        # The append phase failed — the ONE failure that may fail the
        # request (ADR-023). Nothing user-visible committed; the response is
        # structured and diagnosable (ADR-011 amendment: class + sanitized
        # message), never a generic 500.
        import traceback
        traceback.print_exc()  # full detail to stderr only
        e = _record_error("mcp.send_chat.append", exc)
        return _tool_failure(
            msg_id, f"message append failed: {e['class']}: {e['message']} "
                    "— nothing was committed; see get_errors / /lab/errors")
    if posted is None:
        return _tool_failure(msg_id, f"no such branch: {branch_id}")
    # The message is committed from here on: whatever happens to the reply
    # phase, the tool result must say so (NEVER an error — ADR-023).
    if posted.get("degraded"):
        # Post-commit upkeep failed — don't block the bounded wait on a
        # store that is already degrading; the reply job still runs on the
        # worker (client fate irrelevant) and get_branch shows it landing.
        _submit_to_worker(
            lambda rt: _chat_collect_reply_safe(rt, posted["message_id"]))
        steps = ", ".join(f"{d['kind']} ({d['class']})"
                          for d in posted["degraded"])
        return _tool_result(msg_id, {
            "status": "reply_pending",
            "detail": (f"message committed but the chat path degraded after "
                       f"the append: {steps}. The reply is queued on the "
                       f"worker — poll get_branch for {branch_id}; see "
                       "get_errors for sanitized details"),
            "degraded": posted["degraded"],
            "branch_id": posted["branch_id"],
            "thread_id": posted["thread_id"],
            "message_id": posted["message_id"],
            "message_event_ids": posted["message_event_ids"],
        })
    try:
        reply = run_on_worker(
            lambda rt: _chat_collect_reply_safe(rt, posted["message_id"]),
            reply_wait)
    except Exception as exc:
        # Bounded-wait timeout: the job stays queued and the worker still
        # produces the reply (ADR-023 decoupling) — report partial success.
        _record_error("mcp.send_chat.reply_wait", exc,
                      posted["message_event_ids"])
        reply = None
    if reply is None:
        return _tool_result(msg_id, {
            "status": "reply_pending",
            "detail": ("message delivered (event ids below); the reply has "
                       "not landed within the bounded wait — poll get_branch "
                       f"for {branch_id} to read it when it arrives"),
            "branch_id": posted["branch_id"],
            "thread_id": posted["thread_id"],
            "message_id": posted["message_id"],
            "message_event_ids": posted["message_event_ids"],
        })
    return _tool_result(msg_id, {
        "status": "ok",
        "reply": reply["content"],
        "event_horizon": reply.get("event_horizon"),
        "branch_id": posted["branch_id"],
        "thread_id": posted["thread_id"],
        "message_id": posted["message_id"],
        "message_event_ids": posted["message_event_ids"],
        "created_event_ids": posted["message_event_ids"]
        + (reply.get("reply_event_ids") or []),
    })


def handle_post(raw: bytes, *, get_rt, lock, run_on_worker,
                rate_limited) -> tuple[int, Optional[dict]]:
    """One streamable-HTTP POST: a single JSON-RPC message in, a single JSON
    response out. Returns (http_status, body_dict_or_None). Caller (the
    kernel handler in lab_server) has already authorized the request."""
    try:
        msg = json.loads(raw or b"")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, _rpc_error(None, -32700, "parse error: body must be JSON")
    if isinstance(msg, list):
        return 400, _rpc_error(None, -32600,
                               "batching is not supported; send one message per POST")
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return 400, _rpc_error(None, -32600, "not a JSON-RPC 2.0 message")

    method = msg.get("method")
    msg_id = msg.get("id")

    if msg_id is None:  # notification (e.g. notifications/initialized): accept
        return 202, None

    if method == "initialize":
        requested = (msg.get("params") or {}).get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return 200, _rpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return 200, _rpc_result(msg_id, {})
    if method == "tools/list":
        return 200, _rpc_result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "send_chat":
            if rate_limited():
                return 429, _rpc_error(msg_id, -32000,
                                       "rate limited (30 mutations/min)")
            return 200, _send_chat(msg_id, args, get_rt=get_rt, lock=lock,
                                   run_on_worker=run_on_worker)
        fn = _READ_TOOLS.get(name)
        if fn is None:
            return 200, _rpc_error(msg_id, -32602, f"unknown tool: {name}")
        rt = get_rt()
        try:
            with lock:
                out = fn(rt, args)
        except ToolError as exc:
            return 200, _tool_failure(msg_id, str(exc))
        return 200, _tool_result(msg_id, out)
    return 200, _rpc_error(msg_id, -32601, f"method not found: {method}")
