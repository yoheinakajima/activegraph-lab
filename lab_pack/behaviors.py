"""Lab pack behaviors — v0.1.

Six small reactive behaviors, no orchestrator (docs/ARCHITECTURE.md):

  ingest    — mission.created / crawl_request source → capability_calls through
              tool_gateway → sources → claim observations. Depth/page capped,
              one progress event (mission patch) per page.
  plan      — llm_behavior. site_claim observation → proposed branch with
              narrated reasoning in the event payload.
  work      — branch activated → core task with routing tags; one event
              boundary later, capability-gap check (a gap is evidence, not an
              error); task done/failed → evaluation marker for interpret.
  interpret — llm_behavior. task-outcome evaluation → summary observation +
              pending promote decision (or follow-up branch).
  gate      — pending decision → approval-request event; approved/rejected →
              outcome applied. Nothing publishes without an approved decision.
  answer    — llm_behavior. inbound comm_message on the lab channel →
              event-horizon-stamped reply from graph state; steering messages
              also write the corresponding object mutation.

Coordination is emergent: the lab never calls domain packs — it writes core
tasks and reacts to what appears in the graph (ADR-006). All fetches go
through tool_gateway (CONTRACT.md).

Registries (convenience caches + dedup, never the source of truth; the
re-entrancy footgun in the builder report makes graph scans unsafe inside
behaviors). Call clear_lab_registry() between fixtures, and rebuild on
resume — replay does not re-fire behaviors:

  _CRAWLS            mission_id → {visited, fetched, queued}
  _CALLS             capability_call_id → {url, depth, mission_id}
  _WEB_PROVIDER_ID   the lab's capability_provider object id
  _PLANNED_OBS       observation ids plan already considered
  _BRANCH_COUNT      non-archived branch count (max_open_branches cap)
  _DISPATCHED        branch ids work already dispatched
  _GAP_CHECKED       task ids the gap check already ran for
  _EVALUATED         task ids already marked with an outcome evaluation
  _PENDING_BY_SUBJECT subject_ref → pending decision id
  _APPLIED_DECISIONS decision ids whose outcome gate already applied
  _APPROVED_PUBLISH  subject_refs with an approved publish decision
  _THREAD_TO_BRANCH  comm_thread id → branch id (discusses cache)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from activegraph.packs import behavior, llm_behavior, load_prompts_from_dir

from .llm import (
    AnswerReply,
    BlogDraft,
    InterpretSummary,
    PlanProposal,
    consume_llm_anomalies,
    is_inert,
    llm_usage,
)
from .seams import (
    clear_seam_cache,
    effective_setting,
    seam_versions_stamp,
)
from .settings import LabSettings

_PROMPTS = {p.name: p.body for p in load_prompts_from_dir(Path(__file__).parent / "prompts")}

# ---------------------------------------------------------------- registries

_CRAWLS: dict[str, dict] = {}
_CALLS: dict[str, dict] = {}
_WEB_PROVIDER_ID: dict[str, str] = {}
_PLANNED_OBS: set[str] = set()
_BRANCH_COUNT: dict[str, int] = {"open": 0}
_DISPATCHED: set[str] = set()
_GAP_CHECKED: set[str] = set()
_EVALUATED: set[str] = set()
_PENDING_BY_SUBJECT: dict[str, str] = {}
_APPLIED_DECISIONS: set[str] = set()
_APPROVED_PUBLISH: set[str] = set()
_THREAD_TO_BRANCH: dict[str, str] = {}
_DRAFTED_OBS: set[str] = set()
_FINDING_EMITTED: set[str] = set()
_SLUGS: set[str] = set()
_PUBLISHED_SLUGS: set[str] = set()
# Editorial machinery (ADR-014): the finding queue and its bookkeeping.
_QUEUED_FINDINGS: dict[str, dict] = {}   # finding obs id → provenance context (5a)
_COVERED_FINDINGS: set[str] = set()      # findings a draft request already covers
_RESEARCH_REQUESTED: set[str] = set()    # branch ids with a research request
_DECIDED_THIN: list[str] = []            # decided branches below the evidence bar
_PENDING_PUBLISH: set[str] = set()       # pending publish decision ids (cap)
_IDLE_LOGGED: dict[str, bool] = {"capped": False}  # one idle obs per cap episode
_BRANCH_EVIDENCE: dict[str, list[str]] = {}  # branch id → supported_by targets
# (BehaviorGraph has no relation iteration, so evidence counting inside
# behaviors goes through this registry, fed by relation.created events.)


def clear_lab_registry() -> None:
    """Reset all in-process caches — between fixtures, or to simulate restart."""
    _CRAWLS.clear()
    _CALLS.clear()
    _WEB_PROVIDER_ID.clear()
    _PLANNED_OBS.clear()
    _BRANCH_COUNT["open"] = 0
    _DISPATCHED.clear()
    _GAP_CHECKED.clear()
    _EVALUATED.clear()
    _PENDING_BY_SUBJECT.clear()
    _APPLIED_DECISIONS.clear()
    _APPROVED_PUBLISH.clear()
    _THREAD_TO_BRANCH.clear()
    _DRAFTED_OBS.clear()
    _FINDING_EMITTED.clear()
    _SLUGS.clear()
    _PUBLISHED_SLUGS.clear()
    _QUEUED_FINDINGS.clear()
    _COVERED_FINDINGS.clear()
    _OBS_PROVENANCE.clear()
    _BRANCH_EVIDENCE.clear()
    _RESEARCH_REQUESTED.clear()
    _DECIDED_THIN.clear()
    _PENDING_PUBLISH.clear()
    _IDLE_LOGGED["capped"] = False
    clear_seam_cache()
    # Seam hot-loads mutate live behavior descriptions; restore file defaults
    # so fixture runs are isolated from each other.
    for b in BEHAVIORS:
        name = getattr(b, "name", "")
        if name in _PROMPTS and getattr(b, "description", None) != _PROMPTS[name]:
            try:
                setattr(b, "description", _PROMPTS[name])
            except Exception:
                object.__setattr__(b, "description", _PROMPTS[name])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_lab_event(graph, event_type: str, payload: dict) -> None:
    """Append a lab marker event (artifact.published, lab.paused, lab.resumed —
    ADR-013/015). Marker events carry no graph-state projection; the lab's own
    projections read them back from the log. Works with both graph flavors:
    BehaviorGraph.emit(type, payload) inside behaviors, Event construction on
    a plain Graph (server paths)."""
    emit = getattr(graph, "emit", None)
    if emit is None:
        return
    try:
        emit(event_type, dict(payload))
        return
    except TypeError:
        pass  # plain Graph: emit(Event)
    try:
        from activegraph.core.event import Event
        emit(Event(id=graph.ids.event(), type=event_type, payload=dict(payload),
                   actor="lab", timestamp=graph.clock.now()))
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers

from html import unescape as _unescape

# Non-content subtrees: their text is chrome, not claims. The live log showed
# nav link rows and SVG path data recorded as "claims" before these were
# dropped (the finding seeded in bundle._seed_findings tells that story).
_DROP_RE = re.compile(
    r"<(script|style|svg|nav|footer)[^>]*>.*?</\1\s*>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)

_CLAIM_CUES = (
    "replay", "audit", "event", "graph", "behavior", "pack", "agent",
    "provides", "enables", "supports", "lets", "every", "automatic",
    "deterministic", "fork", "inspect", "%",
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[.,;:!?)\"']*$")


def _strip_html(html: str) -> str:
    """Readable text only: drop script/style/svg/nav/footer subtrees, strip
    remaining tags, decode entities, collapse whitespace."""
    text = _HTML_RE.sub(" ", _DROP_RE.sub(" ", html))
    return re.sub(r"\s+", " ", _unescape(text)).strip()


def _sentence_like(s: str) -> bool:
    """A claim candidate must read like prose: length-bounded, never JSON,
    no markup fragments, mostly real words, sentence-terminated. The live log
    showed raw fetch envelopes ('{"url": ..., "status": 200...') and SVG path
    data pass the old cue check via its any-digit clause; this is the shape
    gate in front of it."""
    if not (30 <= len(s) <= 360):
        return False
    if any(ch in s for ch in "<>{}"):  # markup / JSON fragments
        return False
    if not s.endswith((".", "!", "?")):
        return False
    try:
        json.loads(s)
        return False  # whole candidate parses as JSON — an envelope, not prose
    except ValueError:
        pass
    words = s.split()
    wordish = sum(1 for w in words if _WORD_RE.match(w))
    return len(words) >= 5 and wordish >= max(4, int(0.6 * len(words)))


def _claims_from_text(text: str, cap: int) -> list[str]:
    """Deterministic claim extraction: sentence-like candidates containing
    claim cues. Rejected candidates are dropped silently — the cleanup is not
    an observation; polluting the log with it would be its own pollution."""
    sentences = [re.sub(r"\s+", " ", s.strip())
                 for s in re.split(r"(?<=[.!?])\s+", text)]
    claims = []
    for s in sentences:
        if not _sentence_like(s):
            continue
        low = s.lower()
        if any(cue in low for cue in _CLAIM_CUES) or re.search(r"\d", s):
            claims.append(s)
        if len(claims) >= cap:
            break
    return claims


def _links_from_html(html: str, base_url: str) -> list[str]:
    base_host = urlparse(base_url).netloc
    out = []
    for href in _HREF_RE.findall(html):
        absolute = urljoin(base_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and parsed.netloc == base_host:
            clean = absolute.split("#")[0].rstrip("/")
            if clean and clean not in out:
                out.append(clean)
    return out


# ---------------------------------------------------------------- ingest


def _ensure_web_provider(graph) -> str:
    """Idempotently create the tool_gateway capability_provider for web fetches."""
    if "id" in _WEB_PROVIDER_ID:
        return _WEB_PROVIDER_ID["id"]
    provider = graph.add_object("capability_provider", {
        "name": "web",
        "kind": "local",
        "description": "Lab web fetcher. All lab fetches go through tool_gateway (CONTRACT).",
        "capabilities": ["fetch_url"],
    })
    _WEB_PROVIDER_ID["id"] = provider.id
    return provider.id


def _queue_fetch(graph, mission_id: str, url: str, depth: int) -> None:
    """Propose a low-risk capability_call; tool_gateway approves and executes."""
    state = _CRAWLS[mission_id]
    clean = url.split("#")[0].rstrip("/")
    if clean in state["visited"] or clean in state["queued"]:
        return
    state["queued"].add(clean)
    provider_id = _ensure_web_provider(graph)
    call = graph.add_object("capability_call", {
        "provider_id": provider_id,
        "provider_name": "web",
        "capability_name": "fetch_url",
        "input_data": {"url": clean},
        "risk_class": "low",
        "status": "proposed",
        "proposed_by": "lab.ingest",
        "proposed_at": _now(),
        "metadata": {"lab_crawl": True, "mission_id": mission_id, "depth": depth},
    })
    _CALLS[call.id] = {"url": clean, "depth": depth, "mission_id": mission_id}


@behavior(
    name="ingest",
    on=["object.created"],
    creates=["capability_provider", "capability_call", "observation"],
)
def ingest(event, graph, ctx, *, settings: LabSettings):
    """Crawl the mission's site through tool_gateway and extract claim evidence.

    On: object.created (mission, status=active) — starts the crawl at target_url.
        object.created (source, kind=crawl_request) — queues one extra URL.
        object.created (source, kind=tool_result from the lab's fetch_url calls)
        — records the page: claim observations (metadata.lab=site_claim,
        grounds relation to the source), same-domain links queued to depth
        crawl_max_depth / page cap crawl_page_cap, and a mission progress patch
        per page (the progress event).

    Creates: capability_provider, capability_call, observation.
    The source objects themselves are created by tool_gateway's result_sourcer —
    that is the emergent path: ingest proposes calls, the gateway executes and
    sources, core's observation_extractor adds its generic observations for free.
    """
    obj = event.payload.get("object", {})
    obj_id = obj.get("id")
    obj_type = obj.get("type")
    data = obj.get("data", {})

    # ── Case 1: mission created → start crawl ─────────────────────────────
    if obj_type == "mission":
        if not settings.crawl_enabled:
            return
        target = (data.get("target_url") or "").strip()
        if not target or data.get("status") != "active":
            return
        _CRAWLS.setdefault(obj_id, {"visited": set(), "fetched": 0, "queued": set()})
        _queue_fetch(graph, obj_id, target, depth=0)
        return

    if obj_type != "source":
        return

    # ── Case 2: explicit crawl request → queue one URL ────────────────────
    if data.get("kind") == "crawl_request":
        meta = data.get("metadata") or {}
        mission_id = meta.get("mission_id")
        url = (data.get("url") or data.get("content") or "").strip()
        if not mission_id or not url:
            return
        _CRAWLS.setdefault(mission_id, {"visited": set(), "fetched": 0, "queued": set()})
        _queue_fetch(graph, mission_id, url, depth=int(meta.get("depth", 0)))
        return

    # ── Case 3: fetched page came back through the gateway ────────────────
    if data.get("kind") != "tool_result":
        return
    call_id = (data.get("metadata") or {}).get("call_id")
    call_info = _CALLS.get(call_id)
    if not call_info:
        return  # Not one of the lab's fetches.

    mission_id = call_info["mission_id"]
    depth = call_info["depth"]
    url = call_info["url"]
    state = _CRAWLS.setdefault(mission_id, {"visited": set(), "fetched": 0, "queued": set()})
    state["queued"].discard(url)
    page_cap = effective_setting(graph, settings, "crawl_page_cap")
    max_depth = effective_setting(graph, settings, "crawl_max_depth")
    max_claims = effective_setting(graph, settings, "max_claims_per_page")
    if url in state["visited"] or state["fetched"] >= page_cap:
        return
    state["visited"].add(url)
    state["fetched"] += 1

    # The fetch handler returns JSON {"url", "status", "content", "error"?};
    # the gateway stores it (sanitized) as the source content.
    content = data.get("content") or ""
    try:
        payload = json.loads(content)
        html = payload.get("content", "")
        fetched_url = payload.get("url") or url
        status = payload.get("status", 200)
        fetch_error = payload.get("error")
    except (json.JSONDecodeError, AttributeError):
        html, fetched_url, status, fetch_error = content, url, 200, None

    # A failed page is evidence, not an error: record it and move on. The
    # crawl never aborts on one page.
    if fetch_error or (isinstance(status, int) and (status == 0 or status >= 400)):
        graph.add_object("observation", {
            "text": (f"Fetch failed for {fetched_url}: status={status}"
                     + (f" ({fetch_error})" if fetch_error else "")
                     + ". The page could not be read from this environment."),
            "confidence": 1.0,
            "source_ids": [obj_id],
            "category": "risk",
            "metadata": {"lab": "fetch_failure", "mission_id": mission_id,
                         "url": fetched_url, "status": status},
        })
        html = ""  # no claims, no links from a failed page

    text = _strip_html(html)

    try:
        # Claim observations — the gap list's raw material.
        for claim in _claims_from_text(text, max_claims):
            obs = graph.add_object("observation", {
                "text": claim,
                "confidence": 0.7,
                "source_ids": [obj_id],
                "category": "fact",
                "metadata": {"lab": "site_claim", "mission_id": mission_id, "url": fetched_url},
            })
            graph.add_relation(obj_id, obs.id, "grounds")

        # Same-domain links, depth- and cap-bounded.
        if depth < max_depth:
            budget = page_cap - state["fetched"] - len(state["queued"])
            for link in _links_from_html(html, fetched_url):
                if budget <= 0:
                    break
                if link not in state["visited"] and link not in state["queued"]:
                    _queue_fetch(graph, mission_id, link, depth + 1)
                    budget -= 1
    except Exception as exc:
        # Per-page isolation: one malformed page never aborts the crawl.
        graph.add_object("observation", {
            "text": f"Page processing failed for {fetched_url}: {type(exc).__name__}: {exc}",
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "fetch_failure", "mission_id": mission_id,
                         "url": fetched_url, "status": status},
        })

    # Progress event per page: a mission patch is a committed, projectable event.
    mission = graph.get_object(mission_id)
    if mission is not None:
        meta = dict(mission.data.get("metadata") or {})
        meta["crawl"] = {
            "fetched": state["fetched"],
            "queued": len(state["queued"]),
            "page_cap": page_cap,
            "last_url": fetched_url,
            "progress_interval_seconds": effective_setting(
                graph, settings, "progress_interval_seconds"),
            "seam_versions": seam_versions_stamp(
                graph, "setting.crawl_page_cap", "setting.crawl_max_depth",
                "setting.max_claims_per_page"),
        }
        graph.patch_object(mission_id, {"metadata": meta})


# ---------------------------------------------------------------- plan


@llm_behavior(
    name="plan",
    on=["object.created"],
    where={"object.type": "observation", "object.data.metadata.lab": "site_claim"},
    description=_PROMPTS["plan"],
    output_schema=PlanProposal,
    model=None,
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["branch"],
    temperature=0.2,
    max_tokens=1024,
    tools=[],
)
def plan(event, graph, ctx, out, *, settings: LabSettings):
    """Propose a branch for a weakly evidenced site claim.

    On: object.created (observation, metadata.lab=site_claim)
    Creates: branch (proposed, gated) + has_branch + supported_by relations.
    The narrated reasoning rides in the branch.created event payload
    (data.metadata.reasoning) — prioritization is prose, never a formula.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    obs_id = obj.get("id")
    data = obj.get("data", {})
    mission_id = (data.get("metadata") or {}).get("mission_id")

    if not obs_id or obs_id in _PLANNED_OBS:
        return
    _PLANNED_OBS.add(obs_id)

    if out is None or not getattr(out, "should_branch", False):
        return
    if _BRANCH_COUNT["open"] >= effective_setting(graph, settings, "max_open_branches"):
        return

    branch = graph.add_object("branch", {
        "title": (out.title or "Untitled branch")[:120],
        "intent": out.intent,
        "status": "proposed",
        "authority": "gated",
        "mission_id": mission_id,
        "metadata": {
            "reasoning": out.reasoning,
            "claim_observation_id": obs_id,
            "proposed_by": "lab.plan",
            "seam_versions": seam_versions_stamp(graph, "prompt.plan"),
        },
    })
    _BRANCH_COUNT["open"] += 1
    if mission_id:
        graph.add_relation(mission_id, branch.id, "has_branch")
    graph.add_relation(branch.id, obs_id, "supported_by")


# ---------------------------------------------------------------- work


def _routing_for_intent(intent: str) -> dict[str, Any]:
    """Routing convention for emergent dispatch (ADR-006).

    OPEN (docs/ARCHITECTURE.md): exact tag convention. Current shape:
    task.metadata.routing = {"domain": ..., "capability": ...} plus
    metadata.tags. Verified at the current pin: no upstream pack reacts to
    core tasks, so dispatch surfaces capability gaps — which is evidence.
    """
    low = (intent or "").lower()
    if any(w in low for w in ("code", "repo", "test", "implement")):
        return {"domain": "codebase", "capability": "code_task"}
    return {"domain": "research", "capability": "deep_research"}


def _dispatch_branch(graph, branch_id: str, branch_data: dict, settings: LabSettings) -> None:
    if branch_id in _DISPATCHED:
        return
    _DISPATCHED.add(branch_id)
    intent = branch_data.get("intent") or branch_data.get("title") or ""
    routing = _routing_for_intent(intent)
    task = graph.add_object("task", {
        "title": (branch_data.get("title") or "Lab task")[:120],
        "description": intent,
        "status": "active",
        "priority": "medium",
        "metadata": {
            "routing": routing,
            "tags": ["lab", routing["domain"]],
            "lab_branch_id": branch_id,
            "progress_contract": {
                "interval_seconds": settings.progress_interval_seconds,
                "uninterruptible": False,
            },
        },
    })
    graph.add_relation(branch_id, task.id, "dispatched")
    if settings.dispatch_gap_check:
        # The probe patch lands one event boundary later — by then, any pack
        # reacting to task.created has already mutated the live graph.
        meta = dict(task.data.get("metadata") or {})
        meta["dispatch_probe"] = True
        graph.patch_object(task.id, {"metadata": meta})


def _task_reacted(graph, task_id: str) -> bool:
    """Did any pack react to the task? Reaction = a relation linking work
    products to the task (core convention: executes/generates), in either
    relation-argument convention (ADR-008, lab_pack/compat.py), or a status
    change away from 'active'."""
    from .compat import decode_relation, relation_touches
    try:
        for r in graph.relations():
            rel_type, _, _ = decode_relation(r)
            if rel_type in ("executes", "generates") and relation_touches(r, task_id):
                return True
    except Exception:
        pass
    task = graph.get_object(task_id)
    return bool(task and task.data.get("status") not in ("active", None))


def _gap_check(graph, task_id: str) -> None:
    if task_id in _GAP_CHECKED:
        return
    _GAP_CHECKED.add(task_id)
    if _task_reacted(graph, task_id):
        return
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = task.data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    routing = meta.get("routing") or {}
    obs = graph.add_object("observation", {
        "text": (
            f"Capability gap: no loaded pack reacted to task '{task.data.get('title')}' "
            f"(routing: {routing.get('domain')}.{routing.get('capability')}). "
            "The lab cannot execute this work yet. A gap is evidence, not an error."
        ),
        "confidence": 0.95,
        "category": "risk",
        "metadata": {"lab": "capability_gap", "lab_branch_id": branch_id, "task_id": task_id},
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")
    graph.patch_object(task_id, {"status": "blocked"})


def _mark_task_outcome(graph, task_id: str, status: str) -> None:
    if task_id in _EVALUATED:
        return
    _EVALUATED.add(task_id)
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = task.data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    graph.add_object("evaluation", {
        "subject_id": task_id,
        "subject_type": "task",
        "judgment": "completed_successfully" if status == "done" else "failed",
        "rationale": (meta.get("result_summary") or "")[:500],
        "evaluator": "lab.work",
        "metadata": {"lab": "task_outcome", "lab_branch_id": branch_id, "task_id": task_id},
    })


@behavior(
    name="work",
    on=["object.created", "patch.applied"],
    creates=["task", "observation", "evaluation"],
)
def work(event, graph, ctx, *, settings: LabSettings):
    """Dispatch activated branches as core tasks; record gaps and outcomes.

    On: branch created/patched to status=active → core task with routing tags
        + dispatched relation + a probe patch.
        patch.applied (the probe) → capability-gap check one event boundary
        after dispatch; no reaction → gap observation + task blocked.
        task patched to done/failed → task-outcome evaluation (interpret's
        trigger).
    Creates: task, observation (capability_gap), evaluation (task_outcome).
    The lab never calls a domain pack — packs react to the task or they don't,
    and either way the graph records it (ADR-006).
    """
    if event.type == "object.created":
        obj = event.payload.get("object", {})
        if obj.get("type") == "branch" and obj.get("data", {}).get("status") == "active":
            _dispatch_branch(graph, obj.get("id"), obj.get("data", {}), settings)
        return

    # patch.applied
    target = event.payload.get("target")
    diff = event.payload.get("diff") or {}
    if not target:
        return
    obj = graph.get_object(target)
    if obj is None:
        return

    if obj.type == "branch":
        status = diff.get("status") or {}
        if status.get("new") == "active":
            _dispatch_branch(graph, target, obj.data, settings)
        return

    if obj.type == "task":
        meta = obj.data.get("metadata") or {}
        if not meta.get("lab_branch_id"):
            return
        status = diff.get("status") or {}
        if status.get("new") in ("done", "rejected") or status.get("new") == "failed":
            _mark_task_outcome(graph, target, "done" if status.get("new") == "done" else "failed")
            return
        if "metadata" in diff and meta.get("dispatch_probe") and settings.dispatch_gap_check:
            _gap_check(graph, target)


# ---------------------------------------------------------------- interpret


@llm_behavior(
    name="interpret",
    on=["object.created"],
    where={"object.type": "evaluation", "object.data.metadata.lab": "task_outcome"},
    description=_PROMPTS["interpret"],
    output_schema=InterpretSummary,
    model=None,
    view={
        "around": "event.payload.object.data.metadata.lab_branch_id",
        "depth": 1,
        "recent_events": 0,
    },
    creates=["observation", "decision", "branch"],
    temperature=0.2,
    max_tokens=1024,
    tools=[],
)
def interpret(event, graph, ctx, out, *, settings: LabSettings):
    """Turn a task outcome into evidence and a gated promote decision.

    On: object.created (evaluation, metadata.lab=task_outcome)
    Creates: observation (interpretation) + supported_by, branch →
    interpreting, decision (promote, pending) — gate takes it from there.
    outcome='follow_up' additionally proposes a child branch.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    task_id = meta.get("task_id")
    if not branch_id or out is None or is_inert(getattr(out, "summary", None)):
        return

    branch = graph.get_object(branch_id)
    if branch is None or branch.data.get("status") in ("decided", "archived"):
        return

    obs = graph.add_object("observation", {
        "text": out.summary,
        "confidence": 0.8,
        "category": "fact",
        "metadata": {"lab": "interpretation", "lab_branch_id": branch_id,
                     "task_id": task_id,
                     "seam_versions": seam_versions_stamp(graph, "prompt.interpret")},
    })
    graph.add_relation(branch_id, obs.id, "supported_by")
    graph.patch_object(branch_id, {"status": "interpreting"})

    evidence = [obs.id]
    if task_id:
        evidence.append(task_id)
    if obj.get("id"):
        evidence.append(obj.get("id"))

    graph.add_object("decision", {
        "subject_ref": branch_id,
        "kind": "promote",
        "status": "pending",
        "rationale": out.summary[:500],
        "evidence_refs": evidence,
        "metadata": {"requested_by": "lab.interpret"},
    })

    if out.outcome == "follow_up" and out.follow_up_intent:
        if _BRANCH_COUNT["open"] < settings.max_open_branches:
            child = graph.add_object("branch", {
                "title": f"Follow-up: {out.follow_up_intent[:100]}",
                "intent": out.follow_up_intent,
                "status": "proposed",
                "authority": "gated",
                "parent_branch_id": branch_id,
                "mission_id": branch.data.get("mission_id"),
                "metadata": {"reasoning": out.summary, "proposed_by": "lab.interpret"},
            })
            _BRANCH_COUNT["open"] += 1
            graph.add_relation(child.id, branch_id, "forked_from")


# ---------------------------------------------------------------- digest
# Editorial machinery (ADR-014): findings accumulate, drafts are requested.
# A draft_request observation is the ONLY trigger draft_writer reacts to;
# its data carries the code-injected context the model drafts from — the
# classification guidance (4a) and per-item provenance (5a) live HERE, in
# prompt-assembly code, never in prompt prose.

_CLASSIFICATION_GUIDANCE = (
    "Classification guidance: post_kind is 'note' for a small post covering "
    "one finding or a digest of accumulated findings; 'research' for a "
    "multi-evidence or multi-branch synthesis from decided branches; 'build' "
    "for a post about constructing the lab itself (runtime, gates, seams, "
    "deploys)."
)

_OBS_PROVENANCE: dict[str, dict] = {}  # obs id → provenance context (5a)


def _observation_provenance(graph, obs_id: str, data: dict, event) -> dict:
    """5a: per-item context injected into every draft request — originating
    branch title, when and by what behavior the observation was created, and
    whether it was seeded or arose from live work. Captured at creation time:
    the triggering event IS the observation's object.created event, so actor
    and timestamp are right there (behaviors cannot scan the log)."""
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    branch = graph.get_object(branch_id) if branch_id else None
    actor = str(getattr(event, "actor", None) or "system")
    origin = ("seeded" if actor == "system"
              else f"live work by the {actor} behavior")
    return {
        "finding_id": obs_id,
        "text": (data.get("text") or "")[:500],
        "branch_id": branch_id,
        "branch_title": (branch.data.get("title") if branch is not None else None),
        "created_at": str(getattr(event, "timestamp", "") or ""),
        "created_by": actor,
        "origin": origin,
        "evidence_refs": list(meta.get("evidence_refs") or []),
    }


def _item_context(graph, ref: str) -> dict:
    """Provenance context for a draft-request item, registry first, graph
    fallback for objects that predate provenance tracking."""
    ctx = _OBS_PROVENANCE.get(ref)
    if ctx:
        return ctx
    o = graph.get_object(ref)
    text = ""
    if o is not None:
        text = (o.data.get("text") or o.data.get("title")
                or o.data.get("rationale") or "")[:300]
    return {"finding_id": ref, "text": text,
            "origin": "unknown (predates provenance tracking)"}


def _drafting_capped(graph, settings: LabSettings, *, operator: bool) -> bool:
    """4c: when the inbox already holds max_drafts_pending publish decisions,
    automatic drafting idles and records ONE observation per cap episode.
    Operator-requested drafts bypass the cap (explicit attention, ADR-014)."""
    if operator:
        return False
    cap = effective_setting(graph, settings, "max_drafts_pending")
    if len(_PENDING_PUBLISH) < cap:
        _IDLE_LOGGED["capped"] = False
        return False
    if not _IDLE_LOGGED["capped"]:
        _IDLE_LOGGED["capped"] = True
        graph.add_object("observation", {
            "text": (f"Drafting idles: {len(_PENDING_PUBLISH)} publish decisions "
                     f"already pending (cap {cap}). The operator's attention is "
                     "also a budget — findings stay queued until the inbox drains."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "drafting_idle",
                         "pending_publish": len(_PENDING_PUBLISH), "cap": cap},
        })
    return True


def _request_draft(graph, *, post_kind_hint: str, item_ids: list[str],
                   requested_by: str, branch_id: Optional[str] = None,
                   mission_id: Optional[str] = None, rationale: str = ""):
    """Create the draft_request observation draft_writer triggers on. Its
    data is the assembled draft context: classification guidance (4a) and
    per-item provenance (5a) — the model can no longer not know where a
    finding came from."""
    contexts = [_item_context(graph, i) for i in item_ids]
    evidence: list[str] = []
    for c in contexts:
        for r in [c.get("finding_id")] + list(c.get("evidence_refs") or []):
            if r and r not in evidence:
                evidence.append(r)
    req = graph.add_object("observation", {
        "text": (f"Draft request ({post_kind_hint}): write one {post_kind_hint} "
                 f"post covering {len(item_ids)} item(s). {rationale} "
                 + _CLASSIFICATION_GUIDANCE),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "draft_request", "draft_request": True,
                     "post_kind_hint": post_kind_hint,
                     "finding_ids": list(item_ids),
                     "evidence_refs": evidence,
                     "findings_context": contexts,
                     "lab_branch_id": branch_id,
                     "mission_id": mission_id,
                     "requested_by": requested_by},
    })
    for i in item_ids:
        _COVERED_FINDINGS.add(i)
        graph.add_relation(req.id, i, "covers")
    return req


@behavior(
    name="digest",
    on=["object.created", "relation.created"],
    creates=["observation"],
)
def digest(event, graph, ctx, *, settings: LabSettings):
    """Queue findings; request ONE combined note when enough accumulate.

    On: object.created (observation). Every lab observation's provenance is
    captured here (actor + timestamp from its own creation event — 5a).
    A finding-tagged observation is QUEUED (a queued_finding relation plus
    the registry), never drafted directly. When unpublished queued findings
    reach setting.digest_min_findings, one note-kind draft_request covers
    them all — unless the pending-publish cap idles drafting (4c).
        relation.created (supported_by) — feeds the branch-evidence registry
    the research threshold counts against (BehaviorGraph cannot iterate
    relations; rebuilt from the graph on resume).
    """
    if event.type == "relation.created":
        from .compat import decode_relation
        rel = event.payload.get("relation") or {}
        try:
            rtype, src, tgt = decode_relation(rel)
        except Exception:
            return
        if rtype == "supported_by":
            bucket = _BRANCH_EVIDENCE.setdefault(src, [])
            if tgt not in bucket:
                bucket.append(tgt)
        return

    obj = event.payload.get("object", {})
    if obj.get("type") != "observation":
        return
    obs_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    if not obs_id or obs_id in _OBS_PROVENANCE:
        return
    if meta.get("lab"):
        _OBS_PROVENANCE[obs_id] = _observation_provenance(graph, obs_id, data, event)
    if not meta.get("finding"):
        return
    _QUEUED_FINDINGS[obs_id] = _OBS_PROVENANCE.get(obs_id) or {"finding_id": obs_id}
    mission_id = meta.get("mission_id")
    if mission_id:
        graph.add_relation(mission_id, obs_id, "queued_finding")
    queued = [f for f in _QUEUED_FINDINGS if f not in _COVERED_FINDINGS]
    if len(queued) < effective_setting(graph, settings, "digest_min_findings"):
        return
    if _drafting_capped(graph, settings, operator=False):
        return
    # If every queued finding came from one branch, the digest belongs to it
    # (post provenance then links the thread); mixed origins stay mission-level.
    branch_ids = {(_QUEUED_FINDINGS.get(f) or {}).get("branch_id") for f in queued}
    sole_branch = branch_ids.pop() if len(branch_ids) == 1 else None
    _request_draft(
        graph, post_kind_hint="note", item_ids=queued,
        requested_by="lab.digest", branch_id=sole_branch, mission_id=mission_id,
        rationale=f"{len(queued)} unpublished findings have accumulated.",
    )


# ---------------------------------------------------------------- gate


def _branch_evidence_ids(graph, branch_id: str) -> list[str]:
    """Evidence objects linked supported_by to a branch (ADR-008 decode).
    Inside behaviors the graph is a restricted BehaviorGraph with no relation
    iteration — there the registry (fed by relation.created events, rebuilt
    on resume) is the source."""
    from .compat import decode_relation
    if hasattr(graph, "relations"):
        out = []
        try:
            for r in graph.relations():
                rel_type, src, tgt = decode_relation(r)
                if rel_type == "supported_by" and src == branch_id:
                    out.append(tgt)
            return out
        except Exception:
            pass
    return list(_BRANCH_EVIDENCE.get(branch_id, []))


def _maybe_research_request(graph, branch_id: str, decision_id: str,
                            rationale: str, settings: LabSettings) -> None:
    """4b: research/build drafts are earned, not automatic. A branch reaching
    decided with >= research_min_evidence linked evidence objects gets ONE
    research draft_request; a thinner decided branch waits until >=2 decided
    branches can be synthesized with combined evidence over the same bar.
    Replaces the old fire-per-finding path."""
    if branch_id in _RESEARCH_REQUESTED:
        return
    min_ev = effective_setting(graph, settings, "research_min_evidence")
    evidence = _branch_evidence_ids(graph, branch_id)
    branch = graph.get_object(branch_id)
    title = branch.data.get("title") if branch else branch_id
    mission_id = branch.data.get("mission_id") if branch else None

    if len(evidence) >= min_ev:
        if _drafting_capped(graph, settings, operator=False):
            return
        _RESEARCH_REQUESTED.add(branch_id)
        _request_draft(
            graph, post_kind_hint="research", item_ids=evidence,
            requested_by="lab.gate", branch_id=branch_id, mission_id=mission_id,
            rationale=(f"Branch '{title}' reached decided with "
                       f"{len(evidence)} evidence objects. {rationale[:200]}"),
        )
        return

    # Thin decided branch: hold for synthesis across >=2 decided branches.
    if branch_id not in _DECIDED_THIN:
        _DECIDED_THIN.append(branch_id)
    if len(_DECIDED_THIN) < 2:
        return
    combined: list[str] = []
    for b in _DECIDED_THIN:
        combined += [e for e in _branch_evidence_ids(graph, b) if e not in combined]
    if len(combined) < min_ev:
        return
    if _drafting_capped(graph, settings, operator=False):
        return
    group = list(_DECIDED_THIN)
    _DECIDED_THIN.clear()
    for b in group:
        _RESEARCH_REQUESTED.add(b)
    _request_draft(
        graph, post_kind_hint="research", item_ids=combined,
        requested_by="lab.gate", branch_id=branch_id, mission_id=mission_id,
        rationale=(f"Synthesis across {len(group)} decided branches with "
                   f"{len(combined)} combined evidence objects."),
    )


def _mirror_path(settings: LabSettings, slug: str) -> Path:
    return Path(settings.drafts_dir) / f"{slug}.md"


def _mark_draft_rejected(graph, artifact_id: str, settings: LabSettings) -> None:
    """Rejected drafts keep their mirror file, prefixed with a REJECTED header.
    OPEN (docs/ARCHITECTURE.md): spec wanted artifact status 'archived', but the
    core artifact enum has no such value and core is not ours to change
    (ADR-005) — 'rejected' is the conservative mapping."""
    artifact = graph.get_object(artifact_id)
    if artifact is None:
        return
    slug = (artifact.data.get("metadata") or {}).get("slug")
    if not slug:
        return
    try:
        path = _mirror_path(settings, slug)
        if path.exists():
            body = path.read_text()
            if not body.startswith("REJECTED"):
                path.write_text(f"REJECTED — publish decision rejected; kept for the record.\n\n{body}")
    except OSError:
        pass


def _publish_artifact(graph, artifact_id: str, decision_id: str) -> None:
    """The publishing last mile (Phase 1, ADR-013): an approved publish
    decision patches the artifact to status=published with a published_at
    stamp and appends an artifact.published marker event. Idempotent — an
    already-published artifact keeps its original timestamp and slug, and no
    second event is emitted. Slug uniqueness is enforced HERE, at publish
    time: a collision with an already-published slug gets a numeric suffix
    (registry-backed; rebuilt from the graph on resume)."""
    artifact = graph.get_object(artifact_id)
    if artifact is None:
        return
    meta = dict(artifact.data.get("metadata") or {})
    if artifact.data.get("status") == "published":
        if meta.get("slug"):
            _PUBLISHED_SLUGS.add(meta["slug"])
        return
    slug = meta.get("slug") or "post"
    base, n = slug, 2
    while slug in _PUBLISHED_SLUGS:
        slug = f"{base}-{n}"
        n += 1
    _PUBLISHED_SLUGS.add(slug)
    published_at = _now()
    meta["slug"] = slug
    meta["published_at"] = published_at
    try:
        graph.patch_object(artifact_id, {"status": "published", "metadata": meta})
    except Exception:
        return
    emit_lab_event(graph, "artifact.published", {
        "artifact_id": artifact_id,
        "slug": slug,
        "title": artifact.data.get("title"),
        "published_at": published_at,
        "decision_id": decision_id,
    })


def _apply_decision(graph, decision_id: str, data: dict, settings: LabSettings) -> None:
    if decision_id in _APPLIED_DECISIONS:
        return
    _APPLIED_DECISIONS.add(decision_id)
    subject = data.get("subject_ref")
    kind = data.get("kind")
    status = data.get("status")
    _PENDING_BY_SUBJECT.pop(subject, None)

    if kind == "promote" and subject:
        new_status = "decided" if status == "approved" else "archived"
        if status == "rejected":
            _BRANCH_COUNT["open"] = max(0, _BRANCH_COUNT["open"] - 1)
        try:
            graph.patch_object(subject, {
                "status": new_status,
                "metadata": {**((graph.get_object(subject).data.get("metadata")) or {}),
                             "decision_id": decision_id},
            })
        except Exception:
            pass
        if status == "approved":
            _maybe_research_request(graph, subject, decision_id,
                                    data.get("rationale") or "", settings)
    elif kind == "publish" and subject:
        _PENDING_PUBLISH.discard(decision_id)
        if status == "approved":
            _APPROVED_PUBLISH.add(subject)
            _publish_artifact(graph, subject, decision_id)
        else:
            try:
                graph.patch_object(subject, {"status": "rejected"})
            except Exception:
                pass
            _mark_draft_rejected(graph, subject, settings)
    elif kind == "self_modify" and subject:
        # The gate treats self_modify exactly like publish: absolute. Approval
        # hot-loads the seam (no restart); graph-code drafts stay dormant
        # behind LAB_ALLOW_GRAPH_CODE regardless (ADR-012).
        artifact = graph.get_object(subject)
        a_kind = artifact.data.get("kind") if artifact else None
        if status == "approved":
            try:
                graph.patch_object(subject, {"status": "approved"})
            except Exception:
                pass
            if a_kind == "seam":
                from .seams import hot_load
                hot_load(graph, subject)
        else:
            # A rejected seam was never active — nothing to unload; the
            # cache keeps serving whatever was approved before (or nothing).
            try:
                graph.patch_object(subject, {"status": "rejected"})
            except Exception:
                pass
    # schema_change / dependency_pin / other: the record itself is the outcome;
    # humans act on it outside the runtime (e.g. bump the pin in pyproject).


@behavior(
    name="gate",
    on=["object.created", "patch.applied"],
    creates=["observation"],
)
def gate(event, graph, ctx, *, settings: LabSettings):
    """Surface pending decisions and apply resolved ones. Enforce publish gating.

    On: decision created (pending) → approval-request event (a decision patch
        stamping approval_requested_at; the feed pins it as the inbox).
        decision patched to approved/rejected → outcome applied (promote →
        branch decided/archived; publish → artifact published/rejected).
        artifact created/patched to status=published WITHOUT an approved
        publish decision → reverted to proposed + violation observation.
    NOTHING publishes or self-modifies without an approved decision.
    """
    if event.type == "object.created":
        obj = event.payload.get("object", {})
        data = obj.get("data", {})
        if obj.get("type") == "decision" and data.get("status") == "pending":
            decision_id = obj.get("id")
            _PENDING_BY_SUBJECT[data.get("subject_ref", "")] = decision_id
            if data.get("kind") == "publish":
                _PENDING_PUBLISH.add(decision_id)  # the drafting cap's counter
            meta = dict(data.get("metadata") or {})
            meta["approval_requested_at"] = _now()
            graph.patch_object(decision_id, {"metadata": meta})
            return
        if obj.get("type") == "artifact" and data.get("status") == "published":
            _revert_unapproved_publish(graph, obj.get("id"))
        return

    target = event.payload.get("target")
    diff = event.payload.get("diff") or {}
    if not target:
        return
    obj = graph.get_object(target)
    if obj is None:
        return

    if obj.type == "decision":
        new_status = (diff.get("status") or {}).get("new")
        if new_status in ("approved", "rejected"):
            _apply_decision(graph, target, obj.data, settings)
        return

    if obj.type == "artifact":
        if (diff.get("status") or {}).get("new") == "published":
            if target not in _APPROVED_PUBLISH:
                _revert_unapproved_publish(graph, target)


def _revert_unapproved_publish(graph, artifact_id: str) -> None:
    graph.patch_object(artifact_id, {"status": "proposed"})
    graph.add_object("observation", {
        "text": (
            f"Gate violation: artifact {artifact_id} was set to published without "
            "an approved publish decision. Reverted to proposed."
        ),
        "confidence": 1.0,
        "category": "risk",
        "metadata": {"lab": "gate_violation", "artifact_id": artifact_id},
    })


# ---------------------------------------------------------------- draft_writer


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "draft").lower()).strip("-")[:60] or "draft"
    base, n = slug, 2
    while slug in _SLUGS:
        slug = f"{base}-{n}"
        n += 1
    _SLUGS.add(slug)
    return slug


_FOOTNOTE_RE = re.compile(r"\[\^[^\]]+\]")

# 5b: first-person process claims — "I was reading…", "during my review…",
# "I examined…" — are narrative about HOW the lab worked. Without an evidence
# ref to a matching event they are invention: the lab's findings were often
# seeded or produced by other behaviors, and the model must not imply
# firsthand process it never performed.
_PROCESS_CLAIM_RE = re.compile(
    r"\bI was (?:reading|reviewing|examining|looking|browsing|crawling|digging)\b"
    r"|\bduring my (?:review|reading|examination|investigation|crawl|research)\b"
    r"|\bI (?:examined|reviewed|inspected|read through|sat down|dug into|"
    r"went through|looked over|combed through)\b",
    re.I)


def _coverage_review(body: str) -> Optional[str]:
    """Claims-coverage check (draft contract): any substantive paragraph with
    zero evidence refs gets flagged in a review note — never silently
    accepted. Extension (5b): first-person process claims without a footnote
    are flagged separately as possible invented narrative."""
    flagged = []
    process_flagged = []
    for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", body or "")):
        if not para or para.startswith("#") or para.startswith("[^"):
            continue
        has_footnote = bool(_FOOTNOTE_RE.search(para))
        if _PROCESS_CLAIM_RE.search(para) and not has_footnote:
            process_flagged.append(i + 1)
        if len(para) < 80:
            continue  # headings, transitions — not claims
        if not has_footnote:
            flagged.append(i + 1)
    notes = []
    if flagged:
        notes.append(
            "> **Review note (claims coverage):** paragraph(s) "
            + ", ".join(map(str, flagged))
            + " carry no evidence footnotes. Verify or cut before approving.")
    if process_flagged:
        notes.append(
            "> **Review note (process claims):** paragraph(s) "
            + ", ".join(map(str, process_flagged))
            + " make first-person process claims (“I was reading…”, "
            "“during my review…”) with no evidence ref to a matching "
            "event — possible invented narrative. The injected draft context "
            "says where each finding actually came from; verify or cut.")
    if not notes:
        return None
    return "\n\n" + "\n\n".join(notes)


@llm_behavior(
    name="draft_writer",
    on=["object.created"],
    where={"object.type": "observation", "object.data.metadata.draft_request": True},
    description=_PROMPTS["draft_writer"],
    output_schema=BlogDraft,
    model=None,
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["artifact", "decision"],
    temperature=0.4,
    max_tokens=4096,
    tools=[],
)
def draft_writer(event, graph, ctx, out, *, settings: LabSettings):
    """Turn a draft request into a blog post draft + a gated publish decision.

    On: object.created (observation, metadata.draft_request=true) — the ONLY
    trigger (ADR-014). Requests come from digest (accumulated findings →
    note), gate (decided branch / synthesis → research), or the operator's
    chat escape hatch. The request's data carries the code-injected draft
    context: classification guidance and per-item provenance (4a/5a).

    Creates: artifact (kind=blog_draft, status=draft, metadata.post_kind,
    mirrored to drafts/<slug>.md — the graph copy is canonical) + produced
    relation + decision (kind=publish, status=pending). The gate does the
    rest: NOTHING publishes without an approved decision.

    Post-generation: claims-coverage check appends a review note for any
    paragraph without evidence footnotes and for first-person process claims
    without a matching evidence ref (5b); a provenance block (branch,
    evidence, event horizon, model, crawl mode) is always appended.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    obs_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}

    if not obs_id or obs_id in _DRAFTED_OBS:
        return
    _DRAFTED_OBS.add(obs_id)

    if out is None or is_inert(getattr(out, "title", None)):
        return
    body = (getattr(out, "body_markdown", None) or "").strip()
    if not body:
        return

    branch_id = meta.get("lab_branch_id")
    finding_ids = list(meta.get("finding_ids") or [])
    evidence = list(meta.get("evidence_refs") or [])
    for f in finding_ids:
        if f not in evidence:
            evidence.append(f)
    if not evidence:
        evidence.append(obs_id)

    post_kind = getattr(out, "post_kind", None) or meta.get("post_kind_hint") or "note"
    if post_kind not in ("note", "research", "build"):
        post_kind = "note"

    title = (out.title or "Untitled lab note").strip()
    slug = _slugify(out.slug or title)

    review = _coverage_review(body)
    if review:
        body += review

    mission_meta = {}
    mission = None
    if branch_id:
        b = graph.get_object(branch_id)
        if b is not None and b.data.get("mission_id"):
            mission = graph.get_object(b.data["mission_id"])
    if mission is None:
        missions = [m for m in [graph.get_object(meta.get("mission_id"))] if m] \
            if meta.get("mission_id") else []
        mission = missions[0] if missions else None
    if mission is not None:
        mission_meta = mission.data.get("metadata") or {}

    crawl_mode = mission_meta.get("crawl_mode", "live")
    model = llm_usage().get("last_model") or "mock"
    provenance = (
        "\n\n---\n"
        "*Provenance:* "
        f"branch `{branch_id or 'mission-level'}` · "
        f"evidence {', '.join(f'`{e}`' for e in evidence)} · "
        f"as of event `{event.id}` · "
        f"model `{model}` · "
        f"crawl `{crawl_mode}`"
        + ("\n\n*Note: this run crawled a synthetic snapshot, not the live "
           "site — treat site claims accordingly.*" if crawl_mode == "synthetic" else "")
    )
    content = f"# {title}\n\n{body}{provenance}\n"

    artifact = graph.add_object("artifact", {
        "kind": "blog_draft",
        "title": title,
        "content": content,
        "format": "markdown",
        "status": "draft",
        "observation_ids": evidence,
        "metadata": {"lab": "blog_draft", "slug": slug,
                     "post_kind": post_kind,
                     "lab_branch_id": branch_id,
                     "finding_id": (finding_ids[0] if finding_ids else obs_id),
                     "finding_ids": finding_ids,
                     "request_id": obs_id,
                     "seam_versions": seam_versions_stamp(graph, "prompt.draft_writer")},
    })
    if branch_id:
        graph.add_relation(branch_id, artifact.id, "produced")

    # Mirror for easy reading; failure is recorded, never fatal.
    try:
        path = _mirror_path(settings, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError as exc:
        graph.add_object("observation", {
            "text": f"Draft mirror write failed for {slug}: {exc}",
            "confidence": 1.0, "category": "risk",
            "metadata": {"lab": "draft_mirror_failure", "artifact_id": artifact.id},
        })

    graph.add_object("decision", {
        "subject_ref": artifact.id,
        "kind": "publish",
        "status": "pending",
        "rationale": f"Publish blog draft '{title}' ({slug}.md).",
        "evidence_refs": evidence,
        "metadata": {"requested_by": "lab.draft_writer", "lab_branch_id": branch_id},
    })


# ---------------------------------------------------------------- answer

_STEER_PAUSE = ("pause",)
_STEER_RESUME = ("resume", "unpause", "reactivate")
_STEER_APPROVE = ("approve",)
_STEER_REJECT = ("reject",)
_STEER_DRAFT = ("draft",)


def _apply_steering(graph, branch_id: str, content: str) -> Optional[str]:
    """Deterministic steering: the reply is fast, the effect lands at this
    event boundary. Returns a short description of the mutation, or None."""
    low = content.lower()
    branch = graph.get_object(branch_id)
    if branch is None:
        return None

    if any(w in low for w in _STEER_PAUSE):
        graph.patch_object(branch_id, {"status": "paused"})
        return "branch paused"
    if any(w in low for w in _STEER_RESUME):
        if branch.data.get("status") != "paused":
            return None
        graph.patch_object(branch_id, {"status": "active"})
        return "branch resumed (status=active)"
    if any(w in low for w in _STEER_DRAFT):
        # 4b escape hatch: the operator can request a draft on anything.
        # Explicit attention — bypasses the pending-publish cap (ADR-014).
        evidence = _branch_evidence_ids(graph, branch_id)
        hint = "research" if branch.data.get("status") == "decided" else "note"
        _request_draft(
            graph, post_kind_hint=hint, item_ids=evidence,
            requested_by="operator", branch_id=branch_id,
            mission_id=branch.data.get("mission_id"),
            rationale=(f"Operator requested a draft on branch "
                       f"'{branch.data.get('title')}': {content[:160]}"),
        )
        return f"draft requested on this branch ({hint}; operator escape hatch)"
    if any(w in low for w in _STEER_APPROVE):
        decision_id = _PENDING_BY_SUBJECT.get(branch_id)
        if decision_id:
            graph.patch_object(decision_id, {"status": "approved"})
            return f"decision {decision_id} approved"
    if any(w in low for w in _STEER_REJECT):
        decision_id = _PENDING_BY_SUBJECT.get(branch_id)
        if decision_id:
            graph.patch_object(decision_id, {"status": "rejected"})
            return f"decision {decision_id} rejected"
    return None


@llm_behavior(
    name="answer",
    on=["object.created"],
    # INVARIANT (ADR-016): this predicate matches the channel, never the
    # message's metadata.source provenance tag. Operator authority is the
    # server-stamped sender (a valid token IS the operator); source is
    # display provenance — operator, operator_via_mcp, and any future
    # operator_via_* tag must all draw a reply. Narrowing this `where` to a
    # literal source tag silently orphans every other operator surface
    # (locked by the thread_equals_branch fixture and scripts/test_mcp.py).
    where={
        "object.type": "comm_message",
        "object.data.channel": "lab",
        "object.data.direction": "inbound",
    },
    description=_PROMPTS["answer"],
    output_schema=AnswerReply,
    model=None,
    view={
        "around": "event.payload.object.data.metadata.lab_branch_id",
        "depth": 1,
        "recent_events": 0,
    },
    creates=["comm_response_candidate"],
    temperature=0.4,
    max_tokens=1024,
    tools=[],
)
def answer(event, graph, ctx, out, *, settings: LabSettings):
    """Reply inside a branch thread from graph state, stamped with its horizon.

    On: object.created (comm_message, channel=lab, direction=inbound). The lab
    channel is scoped in `where` so the (potentially paid) LLM call fires only
    for branch-thread messages — the same precision-scoping the upstream chat
    pack uses. Communication's intent_detector still records a comm_intent for
    the message; this behavior keys off the message itself.

    Creates: comm_response_candidate with an event-horizon stamp ("as of event
    N" = the triggering event — nothing later was visible) and provenance refs.
    Never blocks on running work: it reads only committed graph state.
    Steering messages (pause/resume/approve/reject) ALSO write the mutation —
    reply fast, effect at the event boundary.
    """
    obj = event.payload.get("object", {})
    msg_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    thread_id = data.get("thread_id") or meta.get("thread_id_hint")

    if not branch_id and thread_id:
        branch_id = _THREAD_TO_BRANCH.get(thread_id)
    if not branch_id:
        return  # Not a branch thread — the generic chat pack may still reply.

    consume_llm_anomalies(graph)
    mutation = _apply_steering(graph, branch_id, data.get("content") or "")

    branch = graph.get_object(branch_id)
    reply = (getattr(out, "reply", None) or "").strip() if out is not None else ""
    if not reply or is_inert(reply):
        # Budget or parse trouble — still answer honestly from graph state.
        reply = ("I couldn't produce a model-written reply (LLM budget or "
                 "output-parse issue — recorded as an observation). ")
        if branch is not None:
            reply += (f"Branch “{branch.data.get('title')}” is currently "
                      f"{branch.data.get('status')}.")
    if mutation:
        reply += f"\n\nApplied: {mutation}."
    reply += f"\n\n— as of event {event.id}"

    candidate = graph.add_object("comm_response_candidate", {
        "message_id": msg_id,
        "thread_id": thread_id,
        "channel": data.get("channel") or settings.answer_channel,
        "content": reply,
        "status": "approved" if settings.auto_approve_answers else "proposed",
        "created_by_behavior": "lab.answer",
        "metadata": {
            "event_horizon": str(event.id),
            "seam_versions": seam_versions_stamp(graph, "prompt.answer"),
            "provenance": {
                "branch_id": branch_id,
                "mission_id": (branch.data.get("mission_id") if branch else None),
                "branch_status": (branch.data.get("status") if branch else None),
            },
        },
    })
    graph.add_relation(candidate.id, msg_id, "response_to")


# Registration order is execution order within an event batch.
BEHAVIORS = [ingest, plan, work, interpret, digest, gate, draft_writer, answer]
