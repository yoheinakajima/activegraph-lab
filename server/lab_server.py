#!/usr/bin/env python3
"""activegraph-lab thin server.

Copies the demo_server.py PATTERN from activegraph-packs — only what the lab
needs: /graph, /trace, /chat, /reset, plus GET /lab/feed (a read-only
projection joining lab events with their objects) and POST /lab/decision
(the inbox's approve/reject). Also serves the notebook feed UI from ui/.

No new storage, no new state: every response is computed from the runtime's
event log and graph. SQLite persistence under data/ (override with
ACTIVEGRAPH_DB / ACTIVEGRAPH_MEMORY_DB); port via LAB_PORT (default 7799).

Run:
    python server/lab_server.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ─── runtime singleton ────────────────────────────────────────────────────────

_lock = threading.Lock()
_rt = None
_llm_info: dict = {}
_BOOT_TIME = None  # set in main()


# ─── auth (KERNEL — ADR-010/012) ──────────────────────────────────────────────
# Public GETs are open to everyone; mutations require the operator's bearer
# token, compared with hmac.compare_digest. Token unset → read-only mode.

import hmac
from collections import deque

_RATE_WINDOW_SECONDS = 60
_RATE_MAX_MUTATIONS = 30
_mutation_times: deque = deque()


def _operator_token() -> str:
    return os.environ.get("LAB_OPERATOR_TOKEN", "").strip()


def _mcp_token() -> str:
    """ADR-016: a separate secret for /mcp, revocable independently. It never
    grants inbox or pause authority, and the operator token never opens /mcp
    (strict two-way separation)."""
    return os.environ.get("LAB_MCP_TOKEN", "").strip()


def _check_token(headers, token: str, unset_msg: str) -> tuple[int, str]:
    """Returns (0, '') if authorized; else (http_status, message)."""
    if not token:
        return 403, unset_msg
    supplied = (headers.get("Authorization") or "").strip()
    if not supplied.startswith("Bearer ") or not supplied[7:].strip():
        return 401, "missing bearer token"
    if not hmac.compare_digest(supplied[7:].strip(), token):
        return 403, "invalid token"
    return 0, ""


def _check_bearer(headers) -> tuple[int, str]:
    return _check_token(headers, _operator_token(),
                        "read-only mode: LAB_OPERATOR_TOKEN is not set on the server")


def _check_mcp_bearer(headers) -> tuple[int, str]:
    """/mcp bearer: the legacy LAB_MCP_TOKEN compare runs FIRST (so a token
    that happens to look like a signed blob still authorizes), then ADR-017
    OAuth verification — an HMAC-signed access token minted by /token, keyed
    from LAB_MCP_TOKEN, verified by recomputation (stateless; rotation of
    LAB_MCP_TOKEN revokes everything). A credential shaped like one of our
    signed blobs that fails verification → 401 invalid_token (clients refresh);
    anything else wrong keeps the legacy 403."""
    token = _mcp_token()
    if not token:
        return 403, "mcp disabled: LAB_MCP_TOKEN is not set on the server"
    supplied = (headers.get("Authorization") or "").strip()
    if not supplied.startswith("Bearer ") or not supplied[7:].strip():
        return 401, "missing bearer token"
    supplied = supplied[7:].strip()
    if hmac.compare_digest(supplied, token):
        return 0, ""
    from server import oauth
    if oauth.looks_signed(supplied):
        if oauth.verify_access_token(oauth.derive_key(token), supplied):
            return 0, ""
        return 401, "invalid or expired token"
    return 403, "invalid token"


def _check_mcp_auth(headers, path: str) -> tuple[int, str]:
    """/mcp authorizes via the bearer header; /mcp/<token> accepts the same
    credential as a path segment with identical authority (ADR-016 amendment:
    claude.ai's custom-connector UI cannot send a static header). The URL is
    a credential: the supplied segment is never echoed, logged, or stored —
    access logging is disabled (Handler.log_message) and error bodies carry
    only a fixed message. Rotation = rotate LAB_MCP_TOKEN."""
    if path == "/mcp":
        return _check_mcp_bearer(headers)
    token = _mcp_token()
    if not token:
        return 403, "mcp disabled: LAB_MCP_TOKEN is not set on the server"
    supplied = path[len("/mcp/"):].strip("/")
    if not supplied or not hmac.compare_digest(supplied, token):
        return 401, "invalid token"
    return 0, ""


def _rate_limited() -> bool:
    import time
    now = time.monotonic()
    while _mutation_times and now - _mutation_times[0] > _RATE_WINDOW_SECONDS:
        _mutation_times.popleft()
    if len(_mutation_times) >= _RATE_MAX_MUTATIONS:
        return True
    _mutation_times.append(now)
    return False


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _memory_db_path() -> str:
    """ADR-009 OPEN: memory_gateway's own store stays local-SQLite for now."""
    override = os.environ.get("ACTIVEGRAPH_MEMORY_DB")
    if override:
        return override
    data = _REPO / "data"
    data.mkdir(exist_ok=True)
    return str(data / "lab_memory.sqlite")


def _rebuild_lab_registries(rt) -> None:
    """Replay rebuilds graph state without firing behaviors, so the lab's
    in-process caches must be repopulated from the replayed objects
    (builder-report resume gotcha; same move the demo server makes)."""
    from lab_pack import behaviors as lb

    lb.clear_lab_registry()
    g = rt.graph
    open_count = 0
    for b in g.objects(type="branch"):
        if b.data.get("status") != "archived":
            open_count += 1
    lb._BRANCH_COUNT["open"] = open_count
    for d in g.objects(type="decision"):
        if d.data.get("status") == "pending":
            lb._PENDING_BY_SUBJECT[d.data.get("subject_ref", "")] = d.id
            if d.data.get("kind") == "publish":
                lb._PENDING_PUBLISH.add(d.id)
        elif d.data.get("kind") == "publish" and d.data.get("status") == "approved":
            lb._APPROVED_PUBLISH.add(d.data.get("subject_ref", ""))
        if d.data.get("status") in ("approved", "rejected"):
            lb._APPLIED_DECISIONS.add(d.id)
    # 5a: observation provenance — actor + timestamp from each object.created
    # event (replay rebuilds objects, not the in-process provenance registry).
    created_by: dict[str, tuple[str, str]] = {}
    for e in g.events:
        if str(e.type) == "object.created":
            o_ = e.payload.get("object") or {}
            if o_.get("type") == "observation":
                created_by[o_.get("id")] = (str(e.actor or "system"),
                                            str(e.timestamp or ""))
    for o in g.objects(type="observation"):
        meta = o.data.get("metadata") or {}
        if meta.get("lab") == "capability_gap" and meta.get("task_id"):
            lb._GAP_CHECKED.add(meta["task_id"])
        if meta.get("lab") == "site_claim":
            lb._PLANNED_OBS.add(o.id)
        if meta.get("lab"):
            actor, ts = created_by.get(str(o.id), ("system", ""))
            branch = g.get_object(meta.get("lab_branch_id")) \
                if meta.get("lab_branch_id") else None
            lb._OBS_PROVENANCE[str(o.id)] = {
                "finding_id": str(o.id),
                "text": (o.data.get("text") or "")[:500],
                "branch_id": meta.get("lab_branch_id"),
                "branch_title": (branch.data.get("title") if branch is not None else None),
                "created_at": ts,
                "created_by": actor,
                "origin": ("seeded" if actor == "system"
                           else f"live work by the {actor} behavior"),
                "evidence_refs": list(meta.get("evidence_refs") or []),
            }
        if meta.get("finding"):
            lb._QUEUED_FINDINGS[str(o.id)] = lb._OBS_PROVENANCE.get(str(o.id)) \
                or {"finding_id": str(o.id)}
        if meta.get("lab") == "draft_request":
            for f in meta.get("finding_ids") or []:
                lb._COVERED_FINDINGS.add(f)
            if meta.get("requested_by") == "lab.gate" and meta.get("lab_branch_id"):
                lb._RESEARCH_REQUESTED.add(meta["lab_branch_id"])
    for e in g.objects(type="evaluation"):
        meta = e.data.get("metadata") or {}
        if meta.get("lab") == "task_outcome" and meta.get("task_id"):
            lb._EVALUATED.add(meta["task_id"])
    for a in g.objects(type="artifact"):
        meta = a.data.get("metadata") or {}
        if meta.get("lab") == "blog_draft":
            if meta.get("slug"):
                lb._SLUGS.add(meta["slug"])
                if a.data.get("status") == "published":
                    lb._PUBLISHED_SLUGS.add(meta["slug"])
            if meta.get("request_id"):
                lb._DRAFTED_OBS.add(meta["request_id"])
            elif meta.get("finding_id"):
                # pre-ADR-014 drafts triggered straight off the finding
                lb._DRAFTED_OBS.add(meta["finding_id"])
                lb._COVERED_FINDINGS.add(meta["finding_id"])
            if meta.get("lab_branch_id"):
                lb._FINDING_EMITTED.add(meta["lab_branch_id"])
    for r in g.relations():
        rel_type, src, tgt = _decode_relation(r)
        if rel_type == "discusses":
            lb._THREAD_TO_BRANCH[src] = tgt
        elif rel_type == "dispatched":
            lb._DISPATCHED.add(src)
        elif rel_type == "supported_by":
            bucket = lb._BRANCH_EVIDENCE.setdefault(src, [])
            if tgt not in bucket:
                bucket.append(tgt)
        elif rel_type == "covers":
            lb._COVERED_FINDINGS.add(tgt)


def _build_runtime():
    from activegraph import Runtime
    from lab_pack import storage
    from lab_pack.bundle import build_lab, load_lab_packs
    from lab_pack.llm import select_lab_provider
    from lab_pack.settings import LabSettings

    global _llm_info
    provider, _llm_info = select_lab_provider(settings=LabSettings())
    print(f"[lab_server] LLM: mode={_llm_info['mode']} provider={_llm_info['provider']} "
          f"model={_llm_info.get('model')}", flush=True)

    # ADR-009: backend selection lives in lab_pack/storage.py only. The URL
    # is a credential (ADR-011) — log the backend name, never the URL.
    db = storage.store_url()
    if storage.store_has_run(db):
        mode = "resumed"
        rt = Runtime.load(db, llm_provider=provider)
        load_lab_packs(rt, memory_backend_url=_memory_db_path())
        from lab_pack.tools import register_web_fetch
        register_web_fetch()
        _rebuild_lab_registries(rt)
        from lab_pack import seams
        n = seams.apply_approved(rt.graph)
        if n:
            print(f"[lab_server] seams: re-applied {n} approved prompt seam(s)", flush=True)
        # Findings discovered after first deploy reach a resumed log here
        # (fresh builds get them via _seed_findings; the key dedups).
        from lab_pack.bundle import queue_findings_once
        mission = next(iter(rt.graph.objects(type="mission")), None)
        seed_branch = next(iter(rt.graph.objects(type="branch")), None)
        if mission is not None and seed_branch is not None:
            n_f = queue_findings_once(rt.graph, branch_id=str(seed_branch.id),
                                      mission_id=str(mission.id))
            if n_f:
                rt.run_until_idle()
                rt.save_state()
                print(f"[lab_server] findings: backfilled {n_f} live finding(s)",
                      flush=True)
    else:
        mode = "fresh"
        rt = build_lab(
            llm_provider=provider,
            lab_settings=LabSettings(),
            memory_backend_url=_memory_db_path(),
            persist_to=db,
        )
        rt.run_until_idle()
        rt.save_state()
    from lab_pack import graph_code
    n_gc = graph_code.load_approved_drafts(rt)  # 0 unless LAB_ALLOW_GRAPH_CODE=1
    if n_gc:
        print(f"[lab_server] graph code: {n_gc} approved draft(s) LOADED "
              "(LAB_ALLOW_GRAPH_CODE=1)", flush=True)
    from lab_pack.llm import sync_daily_budget
    used_today = sync_daily_budget(rt)
    pending = sum(1 for d in rt.graph.objects(type="decision")
                  if d.data.get("status") == "pending")
    print(f"[lab_server] llm daily budget: {used_today} used today (UTC)", flush=True)
    print(f"[lab_server] boot: mode={mode} backend={storage.backend()} "
          f"events={len(rt.graph.events)} pending_decisions={pending}", flush=True)
    return rt


# ─── runtime worker (6a) ──────────────────────────────────────────────────────
# SSE needs a ThreadingHTTPServer (a stream holds its connection open), but
# the event store is thread-bound. So ONE worker thread owns the runtime and
# executes every mutation; request threads only read graph state under _lock.

import queue


class _RuntimeWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="lab-runtime")
        self.jobs: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.boot_error: Optional[BaseException] = None

    def run(self) -> None:
        global _rt
        try:
            with _lock:
                _rt = _build_runtime()
        except BaseException as exc:  # boot failure must surface, not hang
            self.boot_error = exc
            self.ready.set()
            return
        self.ready.set()
        while True:
            fn, fut = self.jobs.get()
            try:
                with _lock:
                    fut["result"] = fn(_rt)
            except BaseException as exc:
                fut["error"] = exc
            fut["done"].set()


_worker: Optional[_RuntimeWorker] = None


def _get_rt():
    """Runtime accessor. Starts the worker on first use; in test mode
    (lab_server._rt injected directly, no worker) returns it as-is."""
    global _worker, _rt
    if _rt is not None and _worker is None:
        return _rt  # test mode: smoke/check_ui/test_auth inject _rt directly
    if _worker is None:
        _worker = _RuntimeWorker()
        _worker.start()
    _worker.ready.wait()
    if _worker.boot_error is not None:
        raise _worker.boot_error
    return _rt


def _run_on_worker(fn, timeout: float = 180):
    """Execute a runtime mutation on the owning thread (store affinity).
    Test mode (no worker): run inline under the lock."""
    if _worker is None or not _worker.is_alive():
        with _lock:
            return fn(_rt)
    fut = {"done": threading.Event(), "result": None, "error": None}
    _worker.jobs.put((fn, fut))
    if not fut["done"].wait(timeout):
        raise TimeoutError("runtime worker did not finish in time")
    if fut["error"] is not None:
        raise fut["error"]
    return fut["result"]


# ─── serialization (projection helpers) ───────────────────────────────────────


from lab_pack.compat import decode_relation as _decode_relation  # ADR-008


def _safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(i) for i in obj]
    try:
        return str(obj)
    except Exception:
        return None


def _object_to_dict(o) -> dict:
    return {"id": str(o.id), "type": str(o.type), "data": _safe(o.data)}


def _event_to_dict(e) -> dict:
    return {
        "id": str(e.id),
        "event_type": str(e.type),
        "timestamp": str(e.timestamp) if e.timestamp else None,
        "actor": str(e.actor) if e.actor else None,
        "payload": _safe(e.payload),
    }


# ─── the feed projection ──────────────────────────────────────────────────────


def _shorten(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _branch_of_object(g, obj_type: str, data: dict, obj_id: str) -> Optional[str]:
    if obj_type == "branch":
        return obj_id
    meta = data.get("metadata") or {}
    if meta.get("lab_branch_id"):
        return meta["lab_branch_id"]
    if obj_type == "decision" and str(data.get("subject_ref", "")).startswith("branch#"):
        return data["subject_ref"]
    return None


# One-sentence templates per lab observation kind (C2). These are the FILE
# defaults; template.feed.<kind> seams override them per-entry, resolved
# AS-OF the entry's event (4c replay fidelity) — old entries keep rendering
# with the version that was active when they happened.
# Available fields: {text} {short_text} {url} {status} {behavior}
DEFAULT_TEMPLATES = {
    "site_claim":       "Noticed a claim on {url}: \u201c{short_text}\u201d",
    "capability_gap":   "{short_text}",
    "interpretation":   "Interpreted the results: {short_text}",
    "gate_violation":   "{short_text}",
    "fetch_failure":    "Couldn't fetch {url} (status {status}) — recorded as evidence.",
    "stall":            "{short_text}",
    "llm_budget":       "LLM budget exhausted — stopping model calls cleanly. {short_text}",
    "llm_parse_failure": "Model output didn't parse in {behavior} — salvaged what I could, raw output kept.",
    "finding":          "Logged a finding: \u201c{short_text}\u201d",
    "upstream_friction": "Recorded upstream friction: \u201c{short_text}\u201d",
    "draft_mirror_failure": "{short_text}",
    "synthetic_crawl":  "{short_text}",
    "seam_refused":     "Refused a seam proposal: {short_text}",
    "draft_request":    "Draft requested: {short_text}",
    "drafting_idle":    "{short_text}",
    "behavior_skipped": "{short_text}",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def _seam_template_timeline(g) -> list:
    """Approved template.feed.* seams as (event_index, kind, version, body),
    built from the approval events in the log (4c: as-of-event resolution)."""
    from lab_pack.kernel import kernel_reference
    out = []
    for i, e in enumerate(g.events):
        if str(e.type) != "patch.applied":
            continue
        if ((e.payload.get("diff") or {}).get("status") or {}).get("new") != "approved":
            continue
        a = g.get_object(e.payload.get("target"))
        if a is None or a.data.get("kind") != "seam":
            continue
        meta = a.data.get("metadata") or {}
        name = str(meta.get("seam_name") or "")
        if not name.startswith("template.feed."):
            continue
        body = a.data.get("content") or ""
        if kernel_reference(body):
            continue
        out.append((i, name[len("template.feed."):],
                    int(meta.get("version") or 0), body))
    return out


def _template_at(timeline: list, kind: str, event_index: int) -> Optional[str]:
    best = None
    for idx, k, version, body in timeline:
        if k == kind and idx <= event_index:
            if best is None or idx >= best[0]:
                best = (idx, body)
    return best[1] if best else None


def _narrate_created(g, obj_type: str, data: dict, obj_id: str,
                     tmpl=None) -> Optional[str]:
    """One human sentence per event, template-based (LLM narration deferred).
    Every lab-emitted event type has a template; lab-tagged objects with an
    unknown kind hit the fallback so nothing renders blank. `tmpl(kind)`
    returns a seam-overridden template body or None (as-of the entry event).
    """
    meta = data.get("metadata") or {}
    if obj_type == "mission":
        return f"Mission started: {data.get('title')} — target {data.get('target_url')}."
    if obj_type == "branch":
        why = _shorten(meta.get("reasoning") or data.get("intent") or "", 110)
        return f"Branch {data.get('status', 'proposed')}: “{data.get('title')}”" + (f" — {why}" if why else ".")
    if obj_type == "decision":
        return (f"Approval requested ({data.get('kind')}): {_shorten(data.get('rationale') or '', 110)}")
    if obj_type == "observation":
        kind = meta.get("lab")
        if kind:
            template = (tmpl(kind) if tmpl else None) or DEFAULT_TEMPLATES.get(kind)
            if template is None:  # lab-tagged but unknown — fallback, never blank
                template = "Recorded " + kind.replace("_", " ") + ": “{short_text}”"
            ctx = _SafeDict(
                text=data.get("text") or "",
                short_text=_shorten(data.get("text"),
                                    160 if kind != "site_claim" else 120),
                url=meta.get("url") or "",
                status=meta.get("status"),
                behavior=meta.get("behavior") or "a behavior",
            )
            try:
                return template.format_map(ctx)
            except Exception:
                return _shorten(data.get("text"), 160)
        return None  # generic core observations stay off the feed
    if obj_type == "artifact" and meta.get("lab") == "blog_draft":
        return (f"Drafted a blog post: “{data.get('title')}” ({meta.get('slug')}.md) "
                "— publish approval pending.")
    if obj_type == "artifact" and meta.get("lab") == "seam":
        return (f"Seam proposed: {meta.get('seam_name')} v{meta.get('version')} "
                "— self_modify approval pending.")
    if obj_type == "artifact" and meta.get("lab") == "upstream_issue":
        return f"Drafted an upstream issue: “{data.get('title')}” (publishing gated)."
    if obj_type == "task" and meta.get("lab_branch_id"):
        routing = meta.get("routing") or {}
        return (f"Dispatched work: “{data.get('title')}” "
                f"(routing {routing.get('domain')}.{routing.get('capability')}).")
    if obj_type == "evaluation" and meta.get("lab") == "task_outcome":
        return f"Work {data.get('judgment', '').replace('_', ' ')}: {_shorten(data.get('rationale') or '', 110)}"
    if obj_type == "comm_message" and data.get("channel") == "lab":
        via = (" (via MCP)" if (meta.get("source") == "operator_via_mcp") else "")
        return f"{data.get('sender_ref', 'owner')}{via} said: “{_shorten(data.get('content'), 130)}”"
    if obj_type == "comm_response_candidate" and data.get("created_by_behavior") == "lab.answer":
        return f"Lab replied: “{_shorten(data.get('content'), 150)}”"
    if obj_type == "source" and data.get("kind") == "tool_result":
        cap = (data.get("metadata") or {}).get("capability")
        if cap == "fetch_url":
            return "Fetched a page from the mission site."
    if obj_type == "source" and data.get("kind") == "crawl_request":
        return f"Crawl requested for {data.get('url') or data.get('content')}."
    return None


def _narrate_patch(g, obj, diff: dict) -> Optional[str]:
    status = (diff.get("status") or {})
    if obj.type == "branch" and status.get("new"):
        return f"Branch “{obj.data.get('title')}” moved to {status['new']}."
    if obj.type == "decision" and status.get("new") in ("approved", "rejected"):
        return f"Decision {status['new']}: {_shorten(obj.data.get('rationale') or '', 100)}"
    if obj.type == "task" and status.get("new") in ("done", "rejected", "blocked"):
        word = {"done": "completed", "rejected": "failed", "blocked": "blocked"}[status["new"]]
        return f"Task “{obj.data.get('title')}” {word}."
    if obj.type == "artifact" and status.get("new") in ("published", "rejected", "proposed"):
        word = {"published": "published (decision approved)",
                "rejected": "rejected — file kept for the record",
                "proposed": "reverted to proposed (gate)"}[status["new"]]
        return f"Draft “{obj.data.get('title')}” {word}."
    if obj.type == "mission" and "metadata" in diff:
        crawl = (obj.data.get("metadata") or {}).get("crawl") or {}
        if crawl:
            return (f"Crawl progress: {crawl.get('fetched')}/{crawl.get('page_cap')} pages "
                    f"(last: {crawl.get('last_url')}).")
    return None


def _event_index(event_id: str) -> int:
    try:
        return int(str(event_id).rsplit("_", 1)[-1])
    except ValueError:
        return -1


_KIND_BY_TYPE = {
    "comm_message": "chat", "comm_response_candidate": "chat",
    "observation": "observation", "branch": "branch", "decision": "decision",
    "artifact": "draft", "task": "task", "evaluation": "evaluation",
    "mission": "mission", "source": "crawl",
}


def _build_entries(g, timeline=None) -> list[dict]:
    """Every renderable feed entry, chronological. Shared by /lab/feed,
    /lab/entries (pagination) and /lab/stream (SSE). Each entry carries a
    coarse `kind` for the /lab filter row (3a)."""
    entries: list[dict] = []
    if timeline is None:
        timeline = _seam_template_timeline(g)
    for i, e in enumerate(g.events):
        tmpl = (lambda kind, _i=i: _template_at(timeline, kind, _i))
        sentence = None
        branch_id = None
        kind = None
        if e.type == "object.created":
            obj = e.payload.get("object", {}) or {}
            data = obj.get("data", {}) or {}
            sentence = _narrate_created(g, obj.get("type"), data, obj.get("id"), tmpl)
            branch_id = _branch_of_object(g, obj.get("type"), data, obj.get("id"))
            kind = _KIND_BY_TYPE.get(str(obj.get("type")))
            if obj.get("type") == "comm_message" and not branch_id:
                branch_id = (data.get("metadata") or {}).get("lab_branch_id")
        elif e.type == "patch.applied":
            target = e.payload.get("target")
            obj = g.get_object(target) if target else None
            if obj is not None:
                sentence = _narrate_patch(g, obj, e.payload.get("diff") or {})
                branch_id = _branch_of_object(g, str(obj.type), obj.data, str(obj.id))
                kind = _KIND_BY_TYPE.get(str(obj.type))
        elif e.type == "artifact.published":
            # Marker events (ADR-013/015) are pure log entries — narrate here.
            slug = e.payload.get("slug")
            sentence = (f"Published: “{e.payload.get('title') or slug}” "
                        f"→ /posts/{slug}")
            kind = "publish"
            a = g.get_object(e.payload.get("artifact_id"))
            if a is not None:
                branch_id = (a.data.get("metadata") or {}).get("lab_branch_id")
        elif e.type == "lab.paused":
            sentence = "Lab paused by the operator — LLM behaviors idle; answer stays live."
            kind = "control"
        elif e.type == "lab.resumed":
            sentence = "Lab resumed by the operator."
            kind = "control"
        if sentence:
            entry = {
                "event_id": str(e.id),
                "timestamp": str(e.timestamp) if e.timestamp else None,
                "branch_id": branch_id,
                "sentence": sentence,
                "kind": kind or "event",
            }
            if e.type == "artifact.published" and e.payload.get("slug"):
                entry["post_url"] = f"/posts/{e.payload['slug']}"
            # B4: blog drafts carry a preview snippet + link into the thread view.
            if e.type == "object.created":
                obj = e.payload.get("object", {}) or {}
                data = obj.get("data", {}) or {}
                if (data.get("metadata") or {}).get("lab") == "blog_draft":
                    body = data.get("content") or ""
                    live = g.get_object(obj.get("id"))
                    live_meta = (live.data.get("metadata") or {}) if live is not None else {}
                    entry["artifact"] = {
                        "id": obj.get("id"),
                        "slug": live_meta.get("slug")
                                or (data.get("metadata") or {}).get("slug"),
                        "title": data.get("title"),
                        "preview": _shorten(body.split("##", 1)[-1], 220),
                        # 3b: once published, the feed entry cross-links the post.
                        "published": bool(live is not None
                                          and live.data.get("status") == "published"),
                    }
            entry["index"] = i
            entries.append(entry)
    return entries


def _pending_decisions(g) -> list[dict]:
    """The inbox projection: pending decisions with their evidence attached
    inline. Shared by /lab/feed and the MCP get_pending_decisions tool."""
    inbox = []
    for d in g.objects(type="decision"):
        if d.data.get("status") != "pending":
            continue
        evidence = []
        for ref in d.data.get("evidence_refs") or []:
            o = g.get_object(ref)
            if o is not None:
                evidence.append({
                    "id": str(o.id),
                    "type": str(o.type),
                    "text": _shorten(o.data.get("text") or o.data.get("title")
                                     or o.data.get("rationale") or "", 200),
                })
        subject = g.get_object(d.data.get("subject_ref"))
        inbox.append({
            "id": str(d.id),
            "kind": d.data.get("kind"),
            "rationale": d.data.get("rationale"),
            "subject_ref": d.data.get("subject_ref"),
            "subject_title": (subject.data.get("title") if subject is not None else None),
            "branch_id": _branch_of_object(g, "decision", d.data, d.id),
            "requested_at": (d.data.get("metadata") or {}).get("approval_requested_at"),
            "evidence": evidence,
        })
    return inbox


def _feed(rt, limit: int = 100) -> dict:
    """The notebook feed: lab events joined with their objects, one sentence
    each, grouped by branch, pending decisions pinned on top (the inbox).
    6b: returns the newest `limit` entries; older pages via /lab/entries."""
    g = rt.graph
    all_entries = _build_entries(g)
    total_entries = len(all_entries)
    entries = all_entries[-limit:] if limit and limit > 0 else all_entries
    oldest_rendered = entries[0]["event_id"] if entries else None

    # 3a: resolved decisions, newest first — the inbox's collapsed history.
    resolved = []
    for d in g.objects(type="decision"):
        if d.data.get("status") not in ("approved", "rejected"):
            continue
        subject = g.get_object(d.data.get("subject_ref"))
        resolved.append({
            "id": str(d.id),
            "kind": d.data.get("kind"),
            "status": d.data.get("status"),
            "rationale": _shorten(d.data.get("rationale") or "", 200),
            "subject_ref": d.data.get("subject_ref"),
            "subject_title": (subject.data.get("title") if subject is not None else None),
            "branch_id": _branch_of_object(g, "decision", d.data, d.id),
        })
    resolved.sort(key=lambda x: _event_index(x["id"].replace("#", "_")), reverse=True)

    # The inbox: pending decisions with their evidence attached inline.
    inbox = _pending_decisions(g)

    branches = {str(b.id): {"branch": _object_to_dict(b), "entries": []}
                for b in g.objects(type="branch")}
    mission_entries: list[dict] = []
    for entry in reversed(entries):  # reverse-chron
        bucket = branches.get(entry["branch_id"])
        (bucket["entries"] if bucket else mission_entries).append(entry)

    missions = [_object_to_dict(m) for m in g.objects(type="mission")]
    return {
        "as_of_event": str(g.events[-1].id) if g.events else None,
        "llm": _llm_info,
        "status": _operator_status(),
        "mission": missions[0] if missions else None,
        "inbox": inbox,
        "resolved": resolved,
        "total_entries": total_entries,
        "oldest_rendered": oldest_rendered,
        "mission_entries": mission_entries,
        "branches": sorted(
            branches.values(),
            key=lambda b: b["entries"][0]["event_id"] if b["entries"] else "",
            reverse=True,
        ),
    }


def _operator_status() -> dict:
    """ADR-015 status: paused, calls today/cap, cost today/cap. In-process
    state is authoritative between syncs; all of it rebuilds from the log."""
    from lab_pack.llm import _LLM_STATE
    from lab_pack.settings import LabSettings
    defaults = LabSettings()
    cap = _LLM_STATE.get("cost_cap_override")
    return {
        "paused": bool(_LLM_STATE.get("paused")),
        "llm_calls_today": int(_LLM_STATE.get("daily_used") or 0),
        "llm_calls_cap": defaults.max_llm_calls_per_day,
        "llm_cost_today": round(float(_LLM_STATE.get("daily_cost") or 0), 4),
        "llm_cost_cap": float(cap if cap is not None else defaults.daily_cost_cap_usd),
    }


def _status_line() -> str:
    """Small status line for the blog footer and /lab header (6c):
    live|paused · $today/$cap."""
    try:
        s = _operator_status()
    except Exception:
        return ""
    return (f"{'paused' if s['paused'] else 'live'} · "
            f"${s['llm_cost_today']:.2f}/${s['llm_cost_cap']:.2f} today · ")


# ─── the chat path ────────────────────────────────────────────────────────────


def _chat_post_message(rt, branch_id: str, content: str,
                       source: Optional[str] = None):
    """Phase 1 of the one chat code path: append the operator's message (and
    the thread on first use) and save. Returns the ids a caller can report
    even when the reply phase fails or times out — once this returns, the
    message HAS landed in the log. `source` tags the message's metadata
    (e.g. operator_via_mcp) so the public log distinguishes the human from
    their assistant; either way the sender IS the operator (2b: a valid
    token is the operator — no anonymous write path, and the client does not
    get to choose its identity)."""
    from lab_pack.behaviors import _THREAD_TO_BRANCH
    from lab_pack.llm import sync_daily_budget
    from lab_pack.tools import send_branch_message_fn

    sync_daily_budget(rt)  # 7b: authoritative used-today from the log
    if rt.graph.get_object(branch_id) is None:
        return None
    events_before = len(rt.graph.events)
    thread_id = next((t for t, b in _THREAD_TO_BRANCH.items() if b == branch_id), None)
    thread_id, msg = send_branch_message_fn(
        rt.graph, branch_id, content,
        user_ref="operator", thread_id=thread_id, source=source,
    )
    _save(rt)
    return {"branch_id": branch_id, "thread_id": thread_id,
            "message_id": str(msg.id),
            "message_event_ids": [str(e.id) for e in rt.graph.events[events_before:]]}


def _chat_collect_reply(rt, message_id: str):
    """Phase 2: run behaviors to the next idle boundary and project the
    answer candidate for this message. None = no reply landed (the message
    itself is already committed by phase 1)."""
    events_before = len(rt.graph.events)
    rt.run_until_idle()
    _save(rt)
    cands = [c for c in rt.graph.objects(type="comm_response_candidate")
             if str(c.data.get("message_id")) == str(message_id)
             and c.data.get("created_by_behavior") == "lab.answer"]
    if not cands:
        return None
    return {"content": cands[-1].data.get("content"),
            "event_horizon": (cands[-1].data.get("metadata") or {}).get("event_horizon"),
            "reply_event_ids": [str(e.id) for e in rt.graph.events[events_before:]]}


def _chat_job(rt, branch_id: str, content: str, source: Optional[str] = None):
    """The one chat code path: POST /chat and the MCP send_chat tool both land
    here (ADR-016), composed from the two phases above so MCP can bound the
    reply wait separately from the message append. Runs on the runtime
    worker."""
    posted = _chat_post_message(rt, branch_id, content, source)
    if posted is None:
        return None
    reply = _chat_collect_reply(rt, posted["message_id"])
    return {"content": reply["content"] if reply else "No reply produced.",
            "thread_id": posted["thread_id"],
            "branch_id": posted["branch_id"],
            "event_horizon": reply["event_horizon"] if reply else None,
            "message_id": posted["message_id"],
            "message_event_ids": posted["message_event_ids"],
            "created_event_ids": posted["message_event_ids"]
            + (reply["reply_event_ids"] if reply else [])}


# ─── HTTP handler ─────────────────────────────────────────────────────────────

_UI_DIR = _REPO / "ui"
_MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS: public reads are permissive; mutations are same-origin (no
        # Access-Control-Allow-Origin on POST responses, none preflighted).
        if self.command == "GET":
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg: str, status: int = 500):
        self._send_json({"error": msg}, status)

    def _base_url(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        return f"{proto}://{self.headers.get('Host', 'localhost')}"

    def _mcp_challenge(self) -> str:
        """WWW-Authenticate for /mcp 401s: the Bearer challenge plus the
        RFC 9728 resource_metadata pointer (MCP auth spec) so clients can
        discover the ADR-017 OAuth flow."""
        return ('Bearer resource_metadata='
                f'"{self._base_url()}/.well-known/oauth-protected-resource/mcp"')

    def _send_static(self, rel: str):
        path = (_UI_DIR / rel.lstrip("/")).resolve()
        if not str(path).startswith(str(_UI_DIR)) or not path.is_file():
            self._send_error_json("Not found", 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # Only reads are cross-origin; preflight never green-lights mutations.
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_html(self, body: str, status: int = 200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            # ── the public blog (ADR-013): / is the front door ──────────────
            if path == "/":
                from server import blog
                rt = _get_rt()
                with _lock:
                    page = blog.index_page(rt.graph, status_line=_status_line())
                self._send_html(page)
            elif path.startswith("/posts/"):
                from server import blog
                rt = _get_rt()
                slug = path[len("/posts/"):].strip("/")
                with _lock:
                    page = blog.post_page(rt.graph, slug, status_line=_status_line())
                if page is None:
                    self._send_html("<h1>404</h1><p>No such post.</p>", 404)
                else:
                    self._send_html(page)
            elif path == "/feed.xml":
                from server import blog
                rt = _get_rt()
                proto = self.headers.get("X-Forwarded-Proto", "http")
                host = self.headers.get("Host", "localhost")
                with _lock:
                    body = blog.rss(rt.graph, f"{proto}://{host}").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            # ── the open workshop: the notebook UI lives at /lab ────────────
            elif path in ("/lab", "/lab/"):
                self._send_static("index.html")
            elif path in ("/app.js", "/style.css"):
                self._send_static(path)
            elif path == "/lab/feed":
                from lab_pack.watchdog import check_stalls
                rt = _get_rt()
                _run_on_worker(check_stalls)  # A5: stalled work is released
                limit = int(qs.get("limit", 100))
                with _lock:
                    self._send_json(_feed(rt, limit=limit))
            elif path == "/lab/entries":
                self._handle_entries(qs)
            elif path == "/lab/stream":
                self._handle_stream()
            elif path == "/lab/draft":
                self._handle_draft(qs)
            elif path == "/lab/seams":
                self._handle_seams()
            elif path == "/graph":
                self._handle_graph()
            elif path == "/trace":
                self._handle_trace(qs)
            elif path == "/summary":
                self._handle_summary()
            elif path == "/packs":
                self._handle_packs()
            elif path == "/frames":
                self._send_json({"frames": [], "total": 0})
            elif path in ("/health", "/healthz"):
                self._handle_healthz()
            # ── OAuth 2.1 for the MCP surface (ADR-017) ─────────────────────
            elif path in ("/.well-known/oauth-authorization-server",
                          "/.well-known/oauth-authorization-server/mcp"):
                from server import oauth
                self._send_json(oauth.metadata_authorization_server(self._base_url()))
            elif path in ("/.well-known/oauth-protected-resource",
                          "/.well-known/oauth-protected-resource/mcp"):
                from server import oauth
                self._send_json(oauth.metadata_protected_resource(self._base_url()))
            elif path == "/authorize":
                from server import oauth
                if not _mcp_token():
                    self._send_html("<h1>403</h1><p>mcp disabled: LAB_MCP_TOKEN "
                                    "is not set on the server</p>", 403)
                else:
                    status, page = oauth.authorize_page(qs)
                    self._send_html(page, status)
            elif path == "/mcp" or path.startswith("/mcp/"):
                # Streamable HTTP without an SSE channel: GET is declined,
                # which the MCP spec permits (server-initiated messages are
                # simply unavailable). Clients POST JSON-RPC instead.
                self.send_response(405)
                self.send_header("Allow", "POST")
                body = json.dumps({"error": "POST JSON-RPC 2.0 messages to /mcp"}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_error_json("Not found", 404)
        except Exception:
            traceback.print_exc()  # details to stderr only (ADR-011)
            self._send_error_json("internal error", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if path == "/mcp" or path.startswith("/mcp/"):
            # ADR-016: MCP authorizes with LAB_MCP_TOKEN only — the operator
            # token is refused here, and LAB_MCP_TOKEN is refused everywhere
            # else (strict two-way separation). /mcp/<token> presents the same
            # credential in the path (amendment). Parse errors are the MCP
            # module's to render as JSON-RPC errors, so auth runs on raw bytes.
            status, msg = _check_mcp_auth(self.headers, path)
            if status:
                if status == 401:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", self._mcp_challenge())
                    body_b = json.dumps({"error": msg}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body_b)))
                    self.end_headers()
                    self.wfile.write(body_b)
                else:
                    self._send_error_json(msg, status)
                return
            try:
                from server import mcp
                http_status, payload = mcp.handle_post(
                    raw, get_rt=_get_rt, lock=_lock,
                    run_on_worker=_run_on_worker, rate_limited=_rate_limited)
                if payload is None:  # notification accepted, no body
                    self.send_response(http_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send_json(payload, http_status)
            except Exception:
                traceback.print_exc()  # details to stderr only (ADR-011)
                self._send_error_json("internal error", 500)
            return
        if path in ("/register", "/token", "/authorize"):
            # ADR-017: form-encoded bodies, so they route before the JSON
            # parse below. Same fixed-message discipline as /mcp: no supplied
            # value, code, or token-derived value in any error body.
            try:
                self._handle_oauth_post(path, raw)
            except Exception:
                traceback.print_exc()  # details to stderr only (ADR-011)
                self._send_error_json("internal error", 500)
            return
        body = json.loads(raw) if raw else {}
        try:
            # /reset exists only in dev — in prod it is a 404, no override.
            if path == "/reset":
                if os.environ.get("LAB_ENV", "dev") == "dev":
                    self._handle_reset()
                else:
                    self._send_error_json("Not found", 404)
                return
            if path in ("/chat", "/lab/decision", "/lab/pause", "/lab/resume"):
                status, msg = _check_bearer(self.headers)
                if status:
                    if status == 401:
                        self.send_response(401)
                        self.send_header("WWW-Authenticate", "Bearer")
                        body_b = json.dumps({"error": msg}).encode()
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body_b)))
                        self.end_headers()
                        self.wfile.write(body_b)
                    else:
                        self._send_error_json(msg, status)
                    return
                if _rate_limited():
                    self._send_error_json("rate limited (30 mutations/min)", 429)
                    return
            if path == "/chat":
                self._handle_chat(body)
            elif path == "/lab/decision":
                self._handle_decision(body)
            elif path in ("/lab/pause", "/lab/resume"):
                self._handle_pause(path == "/lab/pause")
            else:
                self._send_error_json("Not found", 404)
        except Exception as e:
            traceback.print_exc()
            self._send_error_json("internal error", 500)

    # ── GET /lab/entries (6b: "load older") ─────────────────────────────────

    def _handle_entries(self, qs: dict):
        rt = _get_rt()
        limit = max(1, min(int(qs.get("limit", 100)), 500))
        before = qs.get("before")
        branch_id = qs.get("branch_id")
        with _lock:
            entries = _build_entries(rt.graph)
        if before:
            cut = _event_index(before)
            entries = [e for e in entries if e["index"] < cut]
        if branch_id:
            want = None if branch_id == "mission" else branch_id
            entries = [e for e in entries if e["branch_id"] == want]
        page = entries[-limit:]
        self._send_json({
            "entries": list(reversed(page)),  # reverse-chron, like the feed
            "oldest_rendered": page[0]["event_id"] if page else None,
            "more": len(entries) > len(page),
        })

    # ── GET /lab/stream (6a: SSE) ───────────────────────────────────────────

    def _handle_stream(self):
        """Server-sent events: pushes new feed entries as they commit.
        Runs on its own request thread (ThreadingHTTPServer); reads graph
        state under _lock in 2s ticks, heartbeats every ~14s. The UI falls
        back to polling on error."""
        import time
        _get_rt()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            with _lock:
                last_count = len(_rt.graph.events)
            ticks = 0
            while ticks < 1800:  # ~1h max per connection; client reconnects
                time.sleep(2)
                ticks += 1
                rt = _rt  # /reset may swap the runtime
                if rt is None:
                    break
                with _lock:
                    n = len(rt.graph.events)
                    fresh = _build_entries(rt.graph) if n > last_count else []
                if n > last_count:
                    for entry in fresh:
                        if entry["index"] >= last_count:
                            self.wfile.write(
                                f"data: {json.dumps(entry, default=str)}\n\n".encode())
                    last_count = n
                    self.wfile.flush()
                elif ticks % 7 == 0:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ── GET /lab/seams ──────────────────────────────────────────────────────

    def _handle_seams(self):
        """The Seams view (read-only): every self-modification surface, its
        active version, source (file|graph), and pending proposals."""
        from lab_pack import graph_code, seams
        rt = _get_rt()
        with _lock:
            status = seams.seam_status(rt.graph)
            status.update(graph_code.status(rt.graph))
        self._send_json(status)

    # ── GET /healthz ────────────────────────────────────────────────────────

    def _handle_healthz(self):
        import time
        from lab_pack import storage
        rt = _get_rt()
        with _lock:
            events = rt.graph.events
            pending = sum(1 for d in rt.graph.objects(type="decision")
                          if d.data.get("status") == "pending")
            last_ts = str(events[-1].timestamp) if events else None
            n = len(events)
        self._send_json({
            "status": "ok",
            "backend": storage.backend(),
            "event_count": n,
            "last_event_ts": last_ts,
            "pending_decisions": pending,
            "uptime_seconds": int(time.monotonic() - _BOOT_TIME) if _BOOT_TIME else 0,
            "read_only": not _operator_token(),
            "llm": _llm_info,
            **_operator_status(),  # 6c: paused, calls today/cap, cost today/cap
        })

    # ── GET /lab/draft?slug= ────────────────────────────────────────────────

    def _handle_draft(self, qs: dict):
        """Serve a blog_draft artifact's markdown. The graph copy is canonical
        (the drafts/ file is a mirror), so this reads from the graph."""
        rt = _get_rt()
        slug = qs.get("slug")
        if not slug:
            self._send_error_json("slug is required", 400)
            return
        with _lock:
            match = next((a for a in rt.graph.objects(type="artifact")
                          if (a.data.get("metadata") or {}).get("slug") == slug), None)
            if match is None:
                self._send_error_json(f"no draft with slug: {slug}", 404)
                return
            body = (match.data.get("content") or "").encode()
            status = match.data.get("status")
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("X-Draft-Status", str(status))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── GET /graph ──────────────────────────────────────────────────────────

    def _handle_graph(self):
        rt = _get_rt()
        with _lock:
            objects = [_object_to_dict(o) for o in rt.graph.all_objects()]
            relations = []
            for r in rt.graph.all_relations():
                rel_type, src, tgt = _decode_relation(r)
                relations.append({"id": str(r.id), "type": rel_type,
                                  "source_id": src, "target_id": tgt})
        self._send_json({"objects": objects, "relations": relations,
                         "object_count": len(objects), "relation_count": len(relations)})

    # ── GET /trace ──────────────────────────────────────────────────────────

    def _handle_trace(self, qs: dict):
        rt = _get_rt()
        limit = int(qs.get("limit", 200))
        offset = int(qs.get("offset", 0))
        with _lock:
            events = [_event_to_dict(e) for e in rt.graph.events]
        total = len(events)
        self._send_json({"events": events[offset:offset + limit],
                         "total": total, "offset": offset, "limit": limit})

    # ── GET /summary, /packs (Inspector debugging-view compatibility) ───────

    def _handle_summary(self):
        rt = _get_rt()
        with _lock:
            objects = rt.graph.all_objects()
            counts: dict[str, int] = {}
            for o in objects:
                counts[str(o.type)] = counts.get(str(o.type), 0) + 1
            self._send_json({
                "object_count": len(objects),
                "relation_count": len(rt.graph.all_relations()),
                "event_count": len(rt.graph.events),
                "pack_count": len(rt.loaded_packs()),
                "frame_count": 0,
                "by_type": [{"type": t, "pack": "", "count": n} for t, n in counts.items()],
                "runtime_ready": True,
            })

    def _handle_packs(self):
        rt = _get_rt()
        with _lock:
            packs = []
            for p in rt.loaded_packs():
                packs.append({
                    "name": p.name,
                    "version": str(p.version),
                    "description": getattr(p, "description", None),
                    "object_types": [{"name": ot.name, "description": ot.description}
                                     for ot in p.object_types],
                    "relation_types": [{"name": rt_.name, "description": rt_.description}
                                       for rt_ in p.relation_types],
                    "behaviors": [{"name": b.name,
                                   "trigger": str(b.on[0]) if b.on else None,
                                   "creates": list(b.creates) if b.creates else []}
                                  for b in p.behaviors],
                })
        self._send_json({"packs": packs, "total": len(packs)})

    # ── POST /register, /token, /authorize (OAuth 2.1, ADR-017) ─────────────

    def _handle_oauth_post(self, path: str, raw: bytes):
        """Stateless OAuth: server/oauth.py holds the protocol but no secret —
        the signing key (derived from LAB_MCP_TOKEN) is handed in per call,
        here and only here (kernel). Nothing minted is ever stored or logged;
        the redirect Location and the /token response body are the intended
        delivery channels and the only places a credential appears."""
        from server import oauth
        token = _mcp_token()
        if not token:
            self._send_error_json("mcp disabled: LAB_MCP_TOKEN is not set "
                                  "on the server", 403)
            return
        if _rate_limited():
            self._send_error_json("rate limited (30 mutations/min)", 429)
            return
        key = oauth.derive_key(token)
        if path == "/register":
            status, body = oauth.handle_register(key, raw)
            self._send_json(body, status)
        elif path == "/token":
            status, body = oauth.handle_token(key, oauth.parse_form(raw))
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")  # RFC 6749 §5.1
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:  # /authorize form submit
            status, location, page = oauth.handle_authorize_post(
                key, token, oauth.parse_form(raw))
            if status == 302:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send_html(page, status)

    # ── POST /chat ──────────────────────────────────────────────────────────

    def _handle_chat(self, body: dict):
        """Post a message into a branch's thread; reply comes from the lab's
        answer behavior, stamped with its event horizon."""
        from lab_pack.llm import reset_llm_run_counters

        reset_llm_run_counters()  # A4: per-behavior budget is per run cycle

        rt = _get_rt()
        content = (body.get("content") or "").strip()
        branch_id = body.get("branch_id")
        if not content or not branch_id:
            self._send_error_json("content and branch_id are required", 400)
            return

        out = _run_on_worker(lambda rt: _chat_job(rt, branch_id, content))
        if out is None:
            self._send_error_json(f"no such branch: {branch_id}", 404)
            return
        self._send_json(out)

    # ── POST /lab/decision ──────────────────────────────────────────────────

    def _handle_decision(self, body: dict):
        """Approve/reject a pending decision — the inbox's buttons. The gate
        behavior applies the outcome at the next event boundary."""
        from lab_pack.llm import reset_llm_run_counters
        from lab_pack.tools import approve_decision_fn

        reset_llm_run_counters()
        _get_rt()
        decision_id = body.get("decision_id")
        approved = bool(body.get("approved"))
        if not decision_id:
            self._send_error_json("decision_id is required", 400)
            return
        def job(rt):
            from lab_pack.llm import sync_daily_budget
            sync_daily_budget(rt)
            d = rt.graph.get_object(decision_id)
            if d is None or str(d.type) != "decision":
                return ("error", 404, f"no such decision: {decision_id}")
            if d.data.get("status") != "pending":
                return ("error", 409, f"decision is already {d.data.get('status')}")
            approve_decision_fn(rt.graph, decision_id, approved,
                                body.get("rationale") or "via /lab/decision")
            rt.run_until_idle()
            _save(rt)
            d = rt.graph.get_object(decision_id)
            subject = rt.graph.get_object(d.data.get("subject_ref"))
            return ("ok", {
                "decision_id": decision_id,
                "status": d.data.get("status"),
                "subject_ref": d.data.get("subject_ref"),
                "subject_status": subject.data.get("status") if subject is not None else None,
            })

        out = _run_on_worker(job)
        if out[0] == "error":
            self._send_error_json(out[2], out[1])
            return
        self._send_json(out[1])

    # ── POST /lab/pause, /lab/resume (ADR-015) ──────────────────────────────

    def _handle_pause(self, paused: bool):
        """Flip the global pause: a lab.paused/lab.resumed marker event is the
        durable state (rebuilt from the log at boot, like the daily cap)."""
        from lab_pack.llm import lab_paused, set_lab_paused
        _get_rt()

        def job(rt):
            if lab_paused() == paused:
                return {"paused": paused, "changed": False}
            set_lab_paused(rt.graph, paused, by="operator")
            _save(rt)
            return {"paused": paused, "changed": True}

        self._send_json(_run_on_worker(job))

    # ── POST /reset ─────────────────────────────────────────────────────────

    def _handle_reset(self):
        from lab_pack import storage

        def job(rt):
            global _rt
            _rt = None
            err = storage.dev_reset()
            if err is None:
                for f in (_memory_db_path(), _memory_db_path() + "-wal",
                          _memory_db_path() + "-shm"):
                    try:
                        if os.path.exists(f):
                            os.remove(f)
                    except OSError:
                        err = f"could not remove: {f}"
            if err:
                return ("error", err)
            _rt = _build_runtime()
            return ("ok", len(_rt.graph.events))

        out = _run_on_worker(job)
        if out[0] == "error":
            self._send_error_json(out[1], 500)
            return
        self._send_json({"status": "reset", "event_count": out[1]})



def _save(rt) -> None:
    """save_state on store-backed runtimes; no-op for ephemeral test runtimes."""
    try:
        rt.save_state()
    except Exception:
        pass

def main() -> None:
    import time
    global _BOOT_TIME
    _BOOT_TIME = time.monotonic()
    port = int(os.environ.get("PORT") or os.environ.get("LAB_PORT") or "7799")
    if not _operator_token():
        print("[lab_server] WARNING: LAB_OPERATOR_TOKEN is not set — the lab is "
              "READ-ONLY. All mutations (chat, decisions) will be refused until "
              "the token is configured.", flush=True)
    _get_rt()  # build/resume before accepting requests
    # ThreadingHTTPServer so SSE streams can hold connections open (6a);
    # store thread-affinity is preserved by the runtime worker, which owns
    # the runtime and executes every mutation.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"[lab_server] listening on http://localhost:{port}  "
          f"(feed UI at /, API: /lab/feed /graph /trace /chat /lab/decision /reset)",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    # Run as a script, this module is `__main__`; server/mcp.py imports it as
    # `server.lab_server`. Alias the two so module globals (_llm_info, the
    # rate limiter deque) are one copy, not divergent twins.
    sys.modules.setdefault("server.lab_server", sys.modules[__name__])
    main()
