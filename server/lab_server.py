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
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def _check_bearer(headers) -> tuple[int, str]:
    """Returns (0, '') if authorized; else (http_status, message)."""
    token = _operator_token()
    if not token:
        return 403, "read-only mode: LAB_OPERATOR_TOKEN is not set on the server"
    supplied = (headers.get("Authorization") or "").strip()
    if not supplied.startswith("Bearer ") or not supplied[7:].strip():
        return 401, "missing bearer token"
    if not hmac.compare_digest(supplied[7:].strip(), token):
        return 403, "invalid token"
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
        elif d.data.get("kind") == "publish" and d.data.get("status") == "approved":
            lb._APPROVED_PUBLISH.add(d.data.get("subject_ref", ""))
        if d.data.get("status") in ("approved", "rejected"):
            lb._APPLIED_DECISIONS.add(d.id)
    for o in g.objects(type="observation"):
        meta = o.data.get("metadata") or {}
        if meta.get("lab") == "capability_gap" and meta.get("task_id"):
            lb._GAP_CHECKED.add(meta["task_id"])
        if meta.get("lab") == "site_claim":
            lb._PLANNED_OBS.add(o.id)
    for e in g.objects(type="evaluation"):
        meta = e.data.get("metadata") or {}
        if meta.get("lab") == "task_outcome" and meta.get("task_id"):
            lb._EVALUATED.add(meta["task_id"])
    for a in g.objects(type="artifact"):
        meta = a.data.get("metadata") or {}
        if meta.get("lab") == "blog_draft":
            if meta.get("slug"):
                lb._SLUGS.add(meta["slug"])
            if meta.get("finding_id"):
                lb._DRAFTED_OBS.add(meta["finding_id"])
            if meta.get("lab_branch_id"):
                lb._FINDING_EMITTED.add(meta["lab_branch_id"])
    for r in g.relations():
        rel_type, src, tgt = _decode_relation(r)
        if rel_type == "discusses":
            lb._THREAD_TO_BRANCH[src] = tgt
        elif rel_type == "dispatched":
            lb._DISPATCHED.add(src)


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
    pending = sum(1 for d in rt.graph.objects(type="decision")
                  if d.data.get("status") == "pending")
    print(f"[lab_server] boot: mode={mode} backend={storage.backend()} "
          f"events={len(rt.graph.events)} pending_decisions={pending}", flush=True)
    return rt


def _get_rt():
    global _rt
    with _lock:
        if _rt is None:
            _rt = _build_runtime()
        return _rt


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


# One-sentence templates per lab observation kind (C2). Anything lab-tagged
# but unmatched falls through to the generic template — nothing renders blank.
_OBS_TEMPLATES = {
    "site_claim":       lambda d, m: f"Noticed a claim on {m.get('url')}: “{_shorten(d.get('text'), 120)}”",
    "capability_gap":   lambda d, m: _shorten(d.get("text"), 160),
    "interpretation":   lambda d, m: f"Interpreted the results: {_shorten(d.get('text'), 130)}",
    "gate_violation":   lambda d, m: _shorten(d.get("text"), 160),
    "fetch_failure":    lambda d, m: (f"Couldn't fetch {m.get('url')} "
                                      f"(status {m.get('status')}) — recorded as evidence."),
    "stall":            lambda d, m: _shorten(d.get("text"), 160),
    "llm_budget":       lambda d, m: f"LLM budget exhausted — stopping model calls cleanly. {_shorten(d.get('text'), 90)}",
    "llm_parse_failure": lambda d, m: (f"Model output didn't parse in "
                                       f"{m.get('behavior') or 'a behavior'} — salvaged what I could, raw output kept."),
    "finding":          lambda d, m: f"Logged a finding: “{_shorten(d.get('text'), 140)}”",
    "upstream_friction": lambda d, m: f"Recorded upstream friction: “{_shorten(d.get('text'), 130)}”",
    "draft_mirror_failure": lambda d, m: _shorten(d.get("text"), 140),
    "synthetic_crawl":  lambda d, m: _shorten(d.get("text"), 160),
}


def _narrate_created(g, obj_type: str, data: dict, obj_id: str) -> Optional[str]:
    """One human sentence per event, template-based (LLM narration deferred).
    Every lab-emitted event type has a template; lab-tagged objects with an
    unknown kind hit the fallback so nothing renders blank."""
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
        if kind in _OBS_TEMPLATES:
            return _OBS_TEMPLATES[kind](data, meta)
        if kind:  # lab-tagged but unknown — fallback, never blank
            return f"Recorded {kind.replace('_', ' ')}: “{_shorten(data.get('text'), 130)}”"
        return None  # generic core observations stay off the feed
    if obj_type == "artifact" and meta.get("lab") == "blog_draft":
        return (f"Drafted a blog post: “{data.get('title')}” ({meta.get('slug')}.md) "
                "— publish approval pending.")
    if obj_type == "artifact" and meta.get("lab") == "upstream_issue":
        return f"Drafted an upstream issue: “{data.get('title')}” (publishing gated)."
    if obj_type == "task" and meta.get("lab_branch_id"):
        routing = meta.get("routing") or {}
        return (f"Dispatched work: “{data.get('title')}” "
                f"(routing {routing.get('domain')}.{routing.get('capability')}).")
    if obj_type == "evaluation" and meta.get("lab") == "task_outcome":
        return f"Work {data.get('judgment', '').replace('_', ' ')}: {_shorten(data.get('rationale') or '', 110)}"
    if obj_type == "comm_message" and data.get("channel") == "lab":
        return f"{data.get('sender_ref', 'owner')} said: “{_shorten(data.get('content'), 130)}”"
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


def _feed(rt) -> dict:
    """The notebook feed: lab events joined with their objects, one sentence
    each, grouped by branch, pending decisions pinned on top (the inbox)."""
    g = rt.graph
    entries: list[dict] = []
    for e in g.events:
        sentence = None
        branch_id = None
        if e.type == "object.created":
            obj = e.payload.get("object", {}) or {}
            data = obj.get("data", {}) or {}
            sentence = _narrate_created(g, obj.get("type"), data, obj.get("id"))
            branch_id = _branch_of_object(g, obj.get("type"), data, obj.get("id"))
            if obj.get("type") == "comm_message" and not branch_id:
                branch_id = (data.get("metadata") or {}).get("lab_branch_id")
        elif e.type == "patch.applied":
            target = e.payload.get("target")
            obj = g.get_object(target) if target else None
            if obj is not None:
                sentence = _narrate_patch(g, obj, e.payload.get("diff") or {})
                branch_id = _branch_of_object(g, str(obj.type), obj.data, str(obj.id))
        if sentence:
            entry = {
                "event_id": str(e.id),
                "timestamp": str(e.timestamp) if e.timestamp else None,
                "branch_id": branch_id,
                "sentence": sentence,
            }
            # B4: blog drafts carry a preview snippet + link into the thread view.
            if e.type == "object.created":
                obj = e.payload.get("object", {}) or {}
                data = obj.get("data", {}) or {}
                if (data.get("metadata") or {}).get("lab") == "blog_draft":
                    body = data.get("content") or ""
                    entry["artifact"] = {
                        "id": obj.get("id"),
                        "slug": (data.get("metadata") or {}).get("slug"),
                        "title": data.get("title"),
                        "preview": _shorten(body.split("##", 1)[-1], 220),
                    }
            entries.append(entry)

    # The inbox: pending decisions with their evidence attached inline.
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
        "mission": missions[0] if missions else None,
        "inbox": inbox,
        "mission_entries": mission_entries,
        "branches": sorted(
            branches.values(),
            key=lambda b: b["entries"][0]["event_id"] if b["entries"] else "",
            reverse=True,
        ),
    }


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

    def do_GET(self):
        path = urlparse(self.path).path
        qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        try:
            if path == "/":
                self._send_static("index.html")
            elif path in ("/app.js", "/style.css"):
                self._send_static(path)
            elif path == "/lab/feed":
                from lab_pack.watchdog import check_stalls
                with _lock:
                    rt = _get_rt_unlocked()
                    check_stalls(rt)  # A5: stalled work is released, never hangs silently
                    self._send_json(_feed(rt))
            elif path == "/lab/draft":
                self._handle_draft(qs)
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
            else:
                self._send_error_json("Not found", 404)
        except Exception:
            traceback.print_exc()  # details to stderr only (ADR-011)
            self._send_error_json("internal error", 500)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        try:
            # /reset exists only in dev — in prod it is a 404, no override.
            if path == "/reset":
                if os.environ.get("LAB_ENV", "dev") == "dev":
                    self._handle_reset()
                else:
                    self._send_error_json("Not found", 404)
                return
            if path in ("/chat", "/lab/decision"):
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
            else:
                self._send_error_json("Not found", 404)
        except Exception as e:
            traceback.print_exc()
            self._send_error_json("internal error", 500)

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

    # ── POST /chat ──────────────────────────────────────────────────────────

    def _handle_chat(self, body: dict):
        """Post a message into a branch's thread; reply comes from the lab's
        answer behavior, stamped with its event horizon."""
        from lab_pack.behaviors import _THREAD_TO_BRANCH
        from lab_pack.llm import reset_llm_run_counters
        from lab_pack.tools import send_branch_message_fn

        reset_llm_run_counters()  # A4: per-behavior budget is per run cycle

        rt = _get_rt()
        content = (body.get("content") or "").strip()
        branch_id = body.get("branch_id")
        if not content or not branch_id:
            self._send_error_json("content and branch_id are required", 400)
            return

        with _lock:
            if rt.graph.get_object(branch_id) is None:
                self._send_error_json(f"no such branch: {branch_id}", 404)
                return
            thread_id = next((t for t, b in _THREAD_TO_BRANCH.items() if b == branch_id), None)
            # 2b: a valid token IS the operator — no anonymous write path, and
            # the client does not get to choose its identity.
            thread_id, msg = send_branch_message_fn(
                rt.graph, branch_id, content,
                user_ref="operator", thread_id=thread_id,
            )
            rt.run_until_idle()
            _save(rt)
            cands = [c for c in rt.graph.objects(type="comm_response_candidate")
                     if c.data.get("message_id") == msg.id
                     and c.data.get("created_by_behavior") == "lab.answer"]
            reply = cands[-1].data.get("content") if cands else "No reply produced."
            horizon = (cands[-1].data.get("metadata") or {}).get("event_horizon") if cands else None
        self._send_json({"content": reply, "thread_id": thread_id,
                         "branch_id": branch_id, "event_horizon": horizon})

    # ── POST /lab/decision ──────────────────────────────────────────────────

    def _handle_decision(self, body: dict):
        """Approve/reject a pending decision — the inbox's buttons. The gate
        behavior applies the outcome at the next event boundary."""
        from lab_pack.llm import reset_llm_run_counters
        from lab_pack.tools import approve_decision_fn

        reset_llm_run_counters()

        rt = _get_rt()
        decision_id = body.get("decision_id")
        approved = bool(body.get("approved"))
        if not decision_id:
            self._send_error_json("decision_id is required", 400)
            return
        with _lock:
            d = rt.graph.get_object(decision_id)
            if d is None or str(d.type) != "decision":
                self._send_error_json(f"no such decision: {decision_id}", 404)
                return
            if d.data.get("status") != "pending":
                self._send_error_json(f"decision is already {d.data.get('status')}", 409)
                return
            approve_decision_fn(rt.graph, decision_id, approved,
                                body.get("rationale") or "via /lab/decision")
            rt.run_until_idle()
            _save(rt)
            d = rt.graph.get_object(decision_id)
            subject = rt.graph.get_object(d.data.get("subject_ref"))
        self._send_json({
            "decision_id": decision_id,
            "status": d.data.get("status"),
            "subject_ref": d.data.get("subject_ref"),
            "subject_status": subject.data.get("status") if subject is not None else None,
        })

    # ── POST /reset ─────────────────────────────────────────────────────────

    def _handle_reset(self):
        from lab_pack import storage
        global _rt
        with _lock:
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
                self._send_error_json(err, 500)
                return
            rt = _get_rt_unlocked()
        self._send_json({"status": "reset", "event_count": len(rt.graph.events)})



def _save(rt) -> None:
    """save_state on store-backed runtimes; no-op for ephemeral test runtimes."""
    try:
        rt.save_state()
    except Exception:
        pass

def _get_rt_unlocked():
    """_get_rt without re-acquiring _lock (callers already hold it)."""
    global _rt
    if _rt is None:
        _rt = _build_runtime()
    return _rt


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
    # Single-threaded on purpose (demo_server pattern): the runtime and its
    # SQLite store are owned by one thread; requests serialize through it.
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[lab_server] listening on http://localhost:{port}  "
          f"(feed UI at /, API: /lab/feed /graph /trace /chat /lab/decision /reset)",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
