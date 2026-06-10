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


# ---------------------------------------------------------------- text helpers

_TAG_RE = re.compile(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)

_CLAIM_CUES = (
    "replay", "audit", "event", "graph", "behavior", "pack", "agent",
    "provides", "enables", "supports", "lets", "every", "automatic",
    "deterministic", "fork", "inspect", "%",
)


def _strip_html(html: str) -> str:
    return _HTML_RE.sub(" ", _TAG_RE.sub(" ", html))


def _claims_from_text(text: str, cap: int) -> list[str]:
    """Deterministic claim extraction: assertive sentences containing claim cues."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) >= 30]
    claims = []
    for s in sentences:
        low = s.lower()
        if any(cue in low for cue in _CLAIM_CUES) or re.search(r"\d", s):
            claims.append(re.sub(r"\s+", " ", s)[:300])
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


# ---------------------------------------------------------------- gate


def _branch_evidence_ids(graph, branch_id: str) -> list[str]:
    """Evidence objects linked supported_by to a branch (ADR-008 decode)."""
    from .compat import decode_relation
    out = []
    try:
        for r in graph.relations():
            rel_type, src, tgt = decode_relation(r)
            if rel_type == "supported_by" and src == branch_id:
                out.append(tgt)
    except Exception:
        pass
    return out


def _emit_branch_finding(graph, branch_id: str, decision_id: str, rationale: str) -> None:
    """A decided branch with >=2 evidence objects is a finding — draft_writer's
    trigger. Emitting it here keeps draft_writer on a single trigger path
    (observation.created with metadata.finding) instead of also pattern-matching
    branch patches, which would fire its LLM call on every patch event."""
    if branch_id in _FINDING_EMITTED:
        return
    evidence = _branch_evidence_ids(graph, branch_id)
    if len(evidence) < 2:
        return
    _FINDING_EMITTED.add(branch_id)
    branch = graph.get_object(branch_id)
    title = branch.data.get("title") if branch else branch_id
    obs = graph.add_object("observation", {
        "text": (f"Finding: branch '{title}' decided. {rationale[:300]}"),
        "confidence": 0.9,
        "category": "fact",
        "metadata": {"lab": "finding", "finding": True,
                     "lab_branch_id": branch_id,
                     "decision_id": decision_id,
                     "evidence_refs": evidence},
    })
    graph.add_relation(branch_id, obs.id, "supported_by")


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
            _emit_branch_finding(graph, subject, decision_id, data.get("rationale") or "")
    elif kind == "publish" and subject:
        if status == "approved":
            _APPROVED_PUBLISH.add(subject)
            try:
                graph.patch_object(subject, {"status": "published"})
            except Exception:
                pass
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


def _coverage_review(body: str) -> Optional[str]:
    """Claims-coverage check (draft contract): any substantive paragraph with
    zero evidence refs gets flagged in a review note — never silently accepted."""
    flagged = []
    for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", body or "")):
        if not para or para.startswith("#") or para.startswith("[^"):
            continue
        if len(para) < 80:
            continue  # headings, transitions — not claims
        if not _FOOTNOTE_RE.search(para):
            flagged.append(i + 1)
    if not flagged:
        return None
    return (
        "\n\n> **Review note (claims coverage):** paragraph(s) "
        + ", ".join(map(str, flagged))
        + " carry no evidence footnotes. Verify or cut before approving."
    )


@llm_behavior(
    name="draft_writer",
    on=["object.created"],
    where={"object.type": "observation", "object.data.metadata.finding": True},
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
    """Turn a finding into a blog post draft + a gated publish decision.

    On: object.created (observation, metadata.finding=true). Branch-decided
    findings arrive on this same trigger because gate emits a finding
    observation when an approved promote decision lands on a branch with >=2
    evidence objects (see _emit_branch_finding).

    Creates: artifact (kind=blog_draft, status=draft, mirrored to
    drafts/<slug>.md — the graph copy is canonical) + produced relation +
    decision (kind=publish, status=pending). The gate does the rest: NOTHING
    publishes without an approved decision.

    Post-generation: claims-coverage check appends a review note for any
    paragraph without evidence footnotes; a provenance block (branch,
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
    evidence = list(meta.get("evidence_refs") or [])
    if obs_id not in evidence:
        evidence.append(obs_id)
    evidence += [s for s in (data.get("source_ids") or []) if s not in evidence]

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
                     "lab_branch_id": branch_id, "finding_id": obs_id,
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
BEHAVIORS = [ingest, plan, work, interpret, gate, draft_writer, answer]
