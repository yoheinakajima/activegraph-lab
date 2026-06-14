"""MCP surface (ADR-016) — streamable HTTP, implemented minimally by hand.

JSON-RPC 2.0 over POST /mcp, single-JSON responses (a mode the streamable
HTTP spec permits; no SSE stream, no sessions). The official Python MCP SDK
was evaluated and declined — its streamable-HTTP transport is ASGI-only and
the lab server is stdlib http.server (ADR-016 records the reasoning).

No new state: every READ tool is a projection of the event log; send_chat
goes through the same code path as POST /chat. Auth (LAB_MCP_TOKEN) and the
rate limiter live in server/lab_server.py (kernel, ADR-012); this module
only sees already-authorized messages.

Tool tiers (ADR-016; get_errors added by ADR-023; ADR-021 expansion):
  READ:     get_status, get_feed, get_branch, get_pending_decisions,
            get_post, list_posts, list_seams, list_branches, get_errors,
            get_log, get_entity — pure projections of public data, fast path.
  OPERATOR: send_chat (tagged source=operator_via_mcp in the public log)
  OPERATOR CONTROL (ADR-021; same authority as send_chat): set_budget,
            pause_lab, resume_lab — REVERSIBLE operational controls,
            categorically unlike promotions; each emits a public control
            event. annotate_decision (ADR-026) attaches a public,
            operator_via_mcp-attributed note to a PENDING decision — it
            does NOT and cannot resolve; annotation is commentary, not
            authority.
  EXCLUDED BY DESIGN: approve/reject of decisions and seam promotion
            remain EXCLUDED from MCP — the inbox stays human-only.
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
    "— it lands in the public log. set_budget and pause_lab/resume_lab are "
    "reversible operator controls (public control events; budgets clamp to a "
    "kernel ceiling). annotate_decision attaches a public pre-review note to "
    "a pending decision — commentary, not authority: the operator's UI "
    "prefills its rationale field from the latest note when they resolve. "
    "Approving/rejecting decisions and seam promotion are deliberately not "
    "available here — the inbox stays human-only (ADR-016/021/026)."
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
        "name": "list_branches",
        "description": "Enumerate branches of inquiry, newest first, optionally "
                       "filtered by status — so proposed branches can be found "
                       "and activated (via send_chat 'activate this branch') "
                       "without hand-fetching ids from the UI. Mirror of "
                       "GET /lab/branches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "description": "proposed | active | decided | "
                                          "archived | all (default all)."},
            },
            "required": [],
        },
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
        "name": "get_log",
        "description": "The FULL event log (the feed shows only narrated "
                       "entries) as one-line rows, newest first — mirror of "
                       "/lab/log. Cursor-paginated: pass the oldest rendered "
                       "event id as `before` for older rows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "before": {"type": "string",
                           "description": "Return rows older than this event id."},
                "limit": {"type": "integer",
                          "description": "Max rows (default 100, max 500)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_entity",
        "description": "Inspect ANY object or event id — mirror of "
                       "/lab/entity. Objects: fields, creation/patch events, "
                       "decoded relations both ways. Events: full payload, "
                       "prev/next ids, onward refs.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string",
                                  "description": "e.g. branch#3 or evt_42"}},
            "required": ["id"],
        },
    },
    {
        "name": "github_read",
        "description": "Read-only GitHub passthrough (ADR-022): the same "
                       "allowlisted tools the research worker uses. ops: "
                       "get_tree (repo, ref?, recursive?), get_file (repo, "
                       "path, ref?), list_commits (repo, path?, limit?), "
                       "list_issues / list_pulls (repo, state?, limit?). "
                       "Repos outside GITHUB_REPO_ALLOWLIST are refused.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "op": {"type": "string",
                       "enum": ["get_tree", "get_file", "list_commits",
                                "list_issues", "list_pulls"]},
                "repo": {"type": "string", "description": "owner/name"},
                "path": {"type": "string"},
                "ref": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
                "recursive": {"type": "boolean"},
            },
            "required": ["op", "repo"],
        },
    },
    {
        "name": "set_budget",
        "description": "OPERATOR CONTROL (ADR-021): set the daily LLM cost "
                       "cap in USD. Clamped to the kernel ceiling "
                       "($100/day — not movable from here). today_only=true "
                       "resets at UTC midnight. Emits a public control event "
                       "recording old → new and scope. Reversible, unlike "
                       "decision approval — which stays human-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount_usd": {"type": "number",
                               "description": "The new daily cap in USD (> 0)."},
                "today_only": {"type": "boolean",
                               "description": "True: applies today (UTC) only."},
            },
            "required": ["amount_usd"],
        },
    },
    {
        "name": "pause_lab",
        "description": "OPERATOR CONTROL (ADR-021): pause the lab — same "
                       "semantics as the UI toggle (every LLM behavior except "
                       "answer idles; a public lab.paused control event is "
                       "appended). Reversible via resume_lab.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "resume_lab",
        "description": "OPERATOR CONTROL (ADR-021): resume the lab — appends "
                       "a public lab.resumed control event and drains queued "
                       "work immediately.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "annotate_decision",
        "description": "OPERATOR (ADR-026): attach a public, "
                       "operator_via_mcp-attributed annotation to a PENDING "
                       "decision — pre-review notes, recommendations, draft "
                       "rationale. It does NOT and cannot resolve: "
                       "approve/reject remain EXCLUDED from MCP; annotation "
                       "is commentary, not authority. When the operator "
                       "later resolves in the UI, pending annotations are "
                       "linked into the resolution's evidence and the "
                       "rationale field is prefilled from the most recent "
                       "annotation (the operator can edit before "
                       "submitting).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "decision_id": {"type": "string",
                                "description": "A PENDING decision id, e.g. decision#7."},
                "note": {"type": "string",
                         "description": "The annotation text (public, lands in the log)."},
            },
            "required": ["decision_id", "note"],
        },
    },
    {
        "name": "send_chat",
        "description": "Post a message into a branch's thread WITH OPERATOR "
                       "AUTHORITY. The message is public and tagged "
                       "source=operator_via_mcp. Reversible steering verbs "
                       "work from here (pause/resume, activate/deactivate, "
                       "draft, recrawl — ADR-025); approve/reject are "
                       "REFUSED for MCP-tagged messages — the inbox stays "
                       "human-only (ADR-016/021). Returns IMMEDIATELY with "
                       "status=accepted and the committed message event ids "
                       "(ADR-034: commit-and-return, no blocking wait, never a "
                       "timeout after a successful append). The lab's reply "
                       "runs on the worker — read it via get_branch.",
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


def _tool_list_branches(rt, args: dict) -> dict:
    """Mirror of GET /lab/branches — same projection function, so the HTTP and
    MCP views cannot drift."""
    from server.lab_server import _branches_projection
    return _branches_projection(rt.graph, args.get("status"))


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


def _tool_get_log(rt, args: dict) -> dict:
    """ADR-021: mirror of GET /lab/log — same projection function, so HTTP
    and MCP cannot drift."""
    from server.lab_server import _log_page
    try:
        limit = max(1, min(int(args.get("limit") or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    return _log_page(rt.graph, args.get("before"), limit)


def _tool_get_entity(rt, args: dict) -> dict:
    """ADR-021: mirror of GET /lab/entity — same projection function."""
    from server.lab_server import _entity_projection
    entity_id = (args.get("id") or "").strip()
    if not entity_id:
        raise ToolError("id is required")
    out = _entity_projection(rt.graph, entity_id)
    if out is None:
        raise ToolError(f"no such entity: {entity_id}")
    return out


_READ_TOOLS = {
    "get_status": _tool_get_status,
    "get_feed": _tool_get_feed,
    "get_branch": _tool_get_branch,
    "get_pending_decisions": _tool_get_pending_decisions,
    "get_post": _tool_get_post,
    "list_posts": _tool_list_posts,
    "list_seams": _tool_list_seams,
    "list_branches": _tool_list_branches,
    "get_errors": _tool_get_errors,
    "get_log": _tool_get_log,
    "get_entity": _tool_get_entity,
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


def _send_chat(msg_id: Any, args: dict, *, get_rt, lock, run_on_worker) -> dict:
    """Commit-and-return (ADR-034). The comm_message append is the ONLY step
    that may fail the request (ADR-023); once it commits, the reply is
    fire-and-forgotten onto the worker and the tool returns IMMEDIATELY with
    status=accepted + the committed message event ids. There is NO bounded
    wait and NEVER a timeout error after a successful append — the recurring
    production timeouts (evt_14234, evt_16799: the mutation committed but the
    operator's call timed out under load) were the bounded wait losing the
    race; there is no wait to lose now. The reply (the answer behavior, or a
    steering verb's confirmation) lands once on the worker and is read via
    get_branch."""
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
    if status is None:
        return _tool_failure(msg_id, f"no such branch: {branch_id}")
    # ADR-027: archived branches ARE chat-able — for the activate verb
    # (operator resurrection); the answer behavior refuses everything else
    # by name and states the archive honestly. The old hard refusal here was
    # the production wall between decision#266's continuation direction and
    # branch#62 (evt_13850): the surface that recorded the teaching could
    # not deliver the resurrection.
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
    # Committed. Fire the reply onto the worker and return at once — client
    # fate is irrelevant to the reply's completion (ADR-023 decoupling).
    _submit_to_worker(
        lambda rt: _chat_collect_reply_safe(rt, posted["message_id"]))
    out = {
        "status": "accepted",
        "detail": ("message committed (event ids below); the reply runs on "
                   f"the worker — poll get_branch for {branch_id} to read it"),
        "branch_id": posted["branch_id"],
        "thread_id": posted["thread_id"],
        "message_id": posted["message_id"],
        "message_event_ids": posted["message_event_ids"],
    }
    if posted.get("degraded"):
        # Post-commit upkeep degraded — still committed, reply still queued;
        # surface the sanitized steps so the caller can read get_errors.
        steps = ", ".join(f"{d['kind']} ({d['class']})"
                          for d in posted["degraded"])
        out["degraded"] = posted["degraded"]
        out["detail"] = (f"message committed but the chat path degraded after "
                         f"the append: {steps}. The reply is queued on the "
                         f"worker — poll get_branch for {branch_id}; see "
                         "get_errors for sanitized details")
    return _tool_result(msg_id, out)


def _github_read(msg_id: Any, args: dict) -> dict:
    """ADR-022: the MCP passthrough calls the SAME handlers the gateway
    registers (one endpoint, one allowlist); allowlist refusals come back
    as tool errors, never 500s."""
    from lab_pack.github_read import GITHUB_CAPABILITIES
    op = (args.get("op") or "").strip()
    fn = GITHUB_CAPABILITIES.get(op)
    if fn is None:
        return _tool_failure(
            msg_id, f"unknown op: {op!r} (want one of "
                    f"{', '.join(sorted(GITHUB_CAPABILITIES))})")
    kwargs = {k: v for k, v in args.items() if k != "op"}
    out = fn(**kwargs)
    if out.get("error") and not out.get("status"):
        return _tool_failure(msg_id, out["error"])
    return _tool_result(msg_id, out)


# ── operator-control tier (ADR-021) ─────────────────────────────────────────
# Same MCP authority as send_chat; rate-limited like every mutation. Each
# control emits a PUBLIC event (lab.budget_set / lab.paused / lab.resumed)
# — these are reversible operational controls, categorically unlike
# promotions. Approve/reject and seam promotion remain EXCLUDED.


def _control_set_budget(msg_id: Any, args: dict, *, get_rt, run_on_worker) -> dict:
    try:
        amount = float(args.get("amount_usd"))
    except (TypeError, ValueError):
        return _tool_failure(msg_id, "amount_usd must be a number")
    if amount <= 0:
        return _tool_failure(msg_id, "amount_usd must be positive")
    today_only = bool(args.get("today_only"))
    get_rt()

    def job(rt):
        from lab_pack.llm import set_operator_budget
        from server.lab_server import _save
        out = set_operator_budget(rt, amount, today_only=today_only,
                                  by="operator_via_mcp")
        _save(rt)
        return out

    return _tool_result(msg_id, run_on_worker(job))


def _control_pause(msg_id: Any, paused: bool, *, get_rt, run_on_worker) -> dict:
    from server.lab_server import _pause_job
    get_rt()
    out = run_on_worker(lambda rt: _pause_job(rt, paused,
                                              by="operator_via_mcp"))
    return _tool_result(msg_id, out)


def _annotate_decision(msg_id: Any, args: dict, *, get_rt, run_on_worker) -> dict:
    """ADR-026: commentary, not authority — the note attaches to a PENDING
    decision and resolution stays in the human operator's inbox. The handler
    can only create an observation and append its id to the decision's
    metadata.annotation_refs; no code path here touches status."""
    decision_id = (args.get("decision_id") or "").strip()
    note = (args.get("note") or "").strip()
    if not decision_id or not note:
        return _tool_failure(msg_id, "decision_id and note are required")
    get_rt()

    def job(rt):
        from lab_pack.tools import annotate_decision_fn
        from server.lab_server import _save
        obs = annotate_decision_fn(rt.graph, decision_id, note)
        rt.run_until_idle()
        _save(rt)
        d = rt.graph.get_object(decision_id)
        meta = d.data.get("metadata") or {}
        return {
            "decision_id": decision_id,
            "status": d.data.get("status"),  # stays pending — by construction
            "annotation_id": str(obs.id),
            "annotation_count": len(meta.get("annotation_refs") or []),
            "note": ("annotation recorded (public, operator_via_mcp). It does "
                     "not resolve the decision — approve/reject stay in the "
                     "operator's inbox (ADR-016/021/026)."),
        }

    try:
        return _tool_result(msg_id, run_on_worker(job))
    except ValueError as exc:
        return _tool_failure(msg_id, str(exc))


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
        if name in ("send_chat", "set_budget", "pause_lab", "resume_lab",
                    "annotate_decision"):
            if rate_limited():
                return 429, _rpc_error(msg_id, -32000,
                                       "rate limited (30 mutations/min)")
            if name == "send_chat":
                return 200, _send_chat(msg_id, args, get_rt=get_rt, lock=lock,
                                       run_on_worker=run_on_worker)
            if name == "set_budget":
                return 200, _control_set_budget(msg_id, args, get_rt=get_rt,
                                                run_on_worker=run_on_worker)
            if name == "annotate_decision":
                return 200, _annotate_decision(msg_id, args, get_rt=get_rt,
                                               run_on_worker=run_on_worker)
            return 200, _control_pause(msg_id, name == "pause_lab",
                                       get_rt=get_rt,
                                       run_on_worker=run_on_worker)
        if name == "github_read":
            # ADR-022: a passthrough to the gateway's read-only handlers —
            # no graph, no lock (network I/O must not hold the runtime).
            return 200, _github_read(msg_id, args)
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
