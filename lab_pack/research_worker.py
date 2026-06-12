"""Lab-local research worker — PLUMBING (ADR-020).

Implements the EXISTING routing contract (ADR-006): reacts to core task
objects routed research.deep_research exactly as the work behavior already
emits them. Dispatch code is untouched; delete this module (or leave
research_worker_enabled at its False default) and the capability-gap path
takes over again — the worker is droppable the day an upstream pack becomes
task-reactive.

Two reactive stages over one log, no orchestrator:

  research_intake  — task.created (routing research.deep_research) → claim
                     (a research_progress observation + an executes relation,
                     so the dispatch gap check sees a reaction) → propose
                     fetch capability_calls through tool_gateway (CONTRACT:
                     all fetches go through the gateway), capped per task by
                     setting.research_fetch_cap. tool_result sources →
                     progress patch per fetch; when the last fetch lands →
                     ONE synthesis-request observation (or task failed when
                     every fetch failed — the error is recorded in the
                     event, never silently dropped).
  research_worker  — llm_behavior on the synthesis request: synthesize with
                     per-claim source attribution (findings citing fetched
                     URLs only; unattributable findings are dropped and
                     counted), write core observations + an evaluation
                     linked to the branch, complete the task. Inert LLM
                     output (budget, pause, parse) fails the task with the
                     reason recorded — errors propagate.

Enablement: research_worker_enabled defaults False in LabSettings so no
embedding, fixture, or test reaches the network by surprise; the server
boot enables it — the live lab always runs the worker. Budgets and pause
ride the LabProviderWrapper like every lab llm_behavior; the model routes
through setting.model.research_worker (ADR-019).

Resume: claimed/synthesized task ids rebuild from research_progress /
research_synthesis observations (server._rebuild_lab_registries). In-flight
fetches do not survive a restart — the stall watchdog releases the task.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from activegraph.packs import behavior, llm_behavior, load_prompts_from_dir

from .github_read import call_spec_for_url
from .llm import ResearchSynthesis, consume_llm_anomalies, is_inert
from .seams import effective_setting, seam_versions_stamp
from .settings import LabSettings

_PROMPT = next(p.body for p in
               load_prompts_from_dir(Path(__file__).parent / "prompts")
               if p.name == "research_worker")

_URL_RE = re.compile(r"https?://[^\s)\"'<>\]]+")
_EXCERPT_CHARS = 1500

# ── registries (caches + dedup; rebuilt on resume, never the truth) ─────────
_TASKS: dict[str, dict] = {}      # task_id → {pending, sources, branch_id, cap}
_CALLS: dict[str, str] = {}       # capability_call id → task_id
_CLAIMED: set[str] = set()        # task ids this worker claimed
_SYNTH_REQUESTED: set[str] = set()  # task ids with a synthesis request
_SYNTHESIZED: set[str] = set()    # task ids the llm stage already handled
_GH_PROVIDER_ID: dict[str, str] = {}  # the github capability_provider


def clear_research_worker_registry() -> None:
    _TASKS.clear()
    _CALLS.clear()
    _CLAIMED.clear()
    _SYNTH_REQUESTED.clear()
    _SYNTHESIZED.clear()
    _GH_PROVIDER_ID.clear()


def _ensure_github_provider(graph) -> str:
    """Idempotently create the tool_gateway capability_provider for the
    read-only GitHub tools (ADR-022, rung 1)."""
    if "id" in _GH_PROVIDER_ID:
        return _GH_PROVIDER_ID["id"]
    provider = graph.add_object("capability_provider", {
        "name": "github",
        "kind": "local",
        "description": ("Read-only GitHub access (ADR-022): allowlisted "
                        "public repos via tool_gateway; never a write."),
        "capabilities": ["get_tree", "get_file", "list_commits",
                         "list_issues", "list_pulls"],
    })
    _GH_PROVIDER_ID["id"] = provider.id
    return provider.id


def task_claimed(task_id: str) -> bool:
    """The dispatch gap check consults this: a claim IS a reaction."""
    return task_id in _CLAIMED


def _clean_url(url: str) -> str:
    return url.split("#")[0].rstrip("/").rstrip(".,;")


def _source_urls(graph, task_data: dict, cap: int) -> list[str]:
    """Candidate sources for a research task, deduped and capped: the
    branch's claim observation URL, the mission's target URL, and any URLs
    written into the task description / claim text (the operator can steer
    sources by mentioning links)."""
    meta = task_data.get("metadata") or {}
    texts = [task_data.get("description") or ""]
    candidates: list[str] = []
    branch = graph.get_object(meta.get("lab_branch_id")) \
        if meta.get("lab_branch_id") else None
    if branch is not None:
        b_meta = branch.data.get("metadata") or {}
        claim = graph.get_object(b_meta.get("claim_observation_id")) \
            if b_meta.get("claim_observation_id") else None
        if claim is not None:
            texts.append(claim.data.get("text") or "")
            c_url = (claim.data.get("metadata") or {}).get("url")
            if c_url:
                candidates.append(c_url)
        mission = graph.get_object(branch.data.get("mission_id")) \
            if branch.data.get("mission_id") else None
        if mission is not None and (mission.data.get("target_url") or "").strip():
            candidates.append(mission.data["target_url"].strip())
    for t in texts:
        candidates.extend(_URL_RE.findall(t))
    out: list[str] = []
    for u in candidates:
        clean = _clean_url(u)
        if clean and clean not in out:
            out.append(clean)
        if len(out) >= cap:
            break
    return out


def _fail_task(graph, task_id: str, error: str) -> None:
    """Task failed, error recorded IN the event (the patch diff carries it)
    — errors propagate; the worker never silently aborts (ADR-020)."""
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = dict(task.data.get("metadata") or {})
    meta["error"] = error[:500]
    meta["result_summary"] = error[:500]
    graph.patch_object(task_id, {"status": "rejected", "metadata": meta})


def _patch_progress(graph, task_id: str, state: dict, last_url: str) -> None:
    """One progress event per fetch result (worker contract: a progress
    event at least every 60s; every external call here is bounded well
    under that, so per-result patching satisfies it)."""
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = dict(task.data.get("metadata") or {})
    meta["research"] = {
        "fetched": len(state["sources"]) + len(state["failed"]),
        "ok": len(state["sources"]),
        "pending": len(state["pending"]),
        "cap": state["cap"],
        "last_url": last_url,
    }
    graph.patch_object(task_id, {"metadata": meta})


@behavior(
    name="research_intake",
    on=["object.created"],
    creates=["capability_provider", "capability_call", "observation"],
)
def research_intake(event, graph, ctx, *, settings: LabSettings):
    """Claim research-routed tasks and gather their sources via tool_gateway.

    On: object.created (task, metadata.routing research.deep_research) —
        claim + propose capped fetch capability_calls.
        object.created (source, kind=tool_result from this worker's calls) —
        collect, progress-patch; last result → synthesis request, or task
        failed when every fetch failed.
    """
    if not settings.research_worker_enabled:
        return
    obj = event.payload.get("object", {})
    obj_id = obj.get("id")
    obj_type = obj.get("type")
    data = obj.get("data", {})

    # ── a routed task appears → claim it ────────────────────────────────────
    if obj_type == "task":
        meta = data.get("metadata") or {}
        routing = meta.get("routing") or {}
        if (routing.get("domain"), routing.get("capability")) != \
                ("research", "deep_research"):
            return  # not ours — the capability-gap path stays untouched
        if not meta.get("lab_branch_id") or obj_id in _CLAIMED:
            return
        _CLAIMED.add(obj_id)
        branch_id = meta.get("lab_branch_id")
        cap = max(1, int(effective_setting(graph, settings, "research_fetch_cap")))
        urls = _source_urls(graph, data, cap)
        # The claim observation is the graph-visible reaction; the dispatch
        # gap check reads the claim REGISTRY (task_claimed) — core's
        # `executes` relation is action→task by schema, not ours to bend.
        graph.add_object("observation", {
            "text": (f"Research worker claimed task '{data.get('title')}': "
                     f"fetching {len(urls)} source(s) through tool_gateway "
                     f"(per-task cap {cap})."),
            "confidence": 1.0,
            "category": "fact",
            "metadata": {"lab": "research_progress", "task_id": obj_id,
                         "lab_branch_id": branch_id, "urls": urls,
                         "fetch_cap": cap},
        })
        if not urls:
            _fail_task(graph, obj_id,
                       "research_worker: no source URLs could be identified "
                       "for this task (no claim URL, mission URL, or links "
                       "in the intent)")
            return
        from .behaviors import _ensure_web_provider, _now
        state = {"pending": set(), "sources": [], "failed": [],
                 "branch_id": branch_id, "cap": cap}
        _TASKS[obj_id] = state
        for url in urls:
            # github.com sources route to the read-only GitHub tools
            # (ADR-022); everything else goes through web.fetch_url. Either
            # way the fetch is a tool_gateway capability_call (CONTRACT).
            provider_name, capability, input_data = call_spec_for_url(url)
            provider_id = (_ensure_web_provider(graph)
                           if provider_name == "web"
                           else _ensure_github_provider(graph))
            call = graph.add_object("capability_call", {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "capability_name": capability,
                "input_data": input_data,
                "risk_class": "low",
                "status": "proposed",
                "proposed_by": "lab.research_worker",
                "proposed_at": _now(),
                "metadata": {"lab_research": True, "task_id": obj_id, "url": url},
            })
            _CALLS[call.id] = obj_id
            state["pending"].add(call.id)
        return

    # ── a fetch result came back through the gateway ────────────────────────
    if obj_type != "source" or data.get("kind") != "tool_result":
        return
    call_id = (data.get("metadata") or {}).get("call_id")
    task_id = _CALLS.get(call_id)
    state = _TASKS.get(task_id) if task_id else None
    if state is None or call_id not in state["pending"]:
        return
    state["pending"].discard(call_id)

    # Decode through the salvaging envelope parser — the gateway truncates
    # stored output at max_output_chars, and a truncated envelope must not
    # degrade into escaped-JSON soup (the 1/30 crawl stall; the diagnosis
    # sits above _parse_fetch_envelope in behaviors.py).
    from .behaviors import _parse_fetch_envelope
    content = data.get("content") or ""
    html, env_url, status, fetch_error = _parse_fetch_envelope(content)
    url = env_url or ""

    if fetch_error or (isinstance(status, int) and (status == 0 or status >= 400)):
        state["failed"].append({"url": url, "status": status,
                                "error": str(fetch_error or f"status {status}")})
    else:
        from .behaviors import _strip_html
        state["sources"].append({
            "url": _clean_url(url),
            "status": status,
            "source_id": obj_id,
            "excerpt": _strip_html(html)[:_EXCERPT_CHARS],
        })
    _patch_progress(graph, task_id, state, url)

    if state["pending"]:
        return

    # Last fetch landed: synthesize, or fail with the errors on the record.
    if not state["sources"]:
        errs = "; ".join(f"{f['url']}: {f['error']}" for f in state["failed"])
        _fail_task(graph, task_id,
                   f"research_worker: all {len(state['failed'])} source "
                   f"fetches failed ({errs})")
        return
    if task_id in _SYNTH_REQUESTED:
        return
    _SYNTH_REQUESTED.add(task_id)
    task = graph.get_object(task_id)
    intent = (task.data.get("description") or "") if task is not None else ""
    graph.add_object("observation", {
        "text": (f"Research synthesis request: synthesize "
                 f"{len(state['sources'])} fetched source(s) for task "
                 f"'{task.data.get('title') if task else task_id}'. Every "
                 "finding must attribute the source URL(s) it rests on."),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "research_synthesis_request",
                     "task_id": task_id,
                     "lab_branch_id": state["branch_id"],
                     "intent": intent[:300],
                     "sources": state["sources"],
                     "failed_fetches": state["failed"]},
    })


@llm_behavior(
    name="research_worker",
    on=["object.created"],
    where={"object.type": "observation",
           "object.data.metadata.lab": "research_synthesis_request"},
    description=_PROMPT,
    output_schema=ResearchSynthesis,
    model=None,  # routes through setting.model.research_worker (ADR-019)
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["observation", "evaluation"],
    max_tokens=2048,
    tools=[],
)
def research_worker(event, graph, ctx, out, *, settings: LabSettings):
    """Synthesize fetched sources into attributed evidence and complete the task.

    Creates: one observation per source-attributed finding (source_ids +
    metadata.source_urls; findings citing no fetched URL are dropped and
    counted), one evaluation linked supported_by to the branch, and the
    task completion patch (work's outcome path takes it from there). Inert
    output → task failed with the reason recorded.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    task_id = meta.get("task_id")
    branch_id = meta.get("lab_branch_id")
    if not task_id or task_id in _SYNTHESIZED:
        return
    _SYNTHESIZED.add(task_id)
    if not settings.research_worker_enabled:
        return

    if out is None or is_inert(getattr(out, "summary", None)) or not out.findings:
        _fail_task(graph, task_id,
                   "research_worker: synthesis produced no usable output "
                   "(LLM budget, pause, or parse failure — see the "
                   "preceding observation)")
        return

    fetched = {s["url"]: s["source_id"] for s in (meta.get("sources") or [])}
    stamp = seam_versions_stamp(graph, "prompt.research_worker")
    written = 0
    dropped = 0
    for f in out.findings:
        urls = [_clean_url(u) for u in (f.source_urls or [])]
        urls = [u for u in urls if u in fetched]
        if not urls or not (f.text or "").strip():
            dropped += 1  # attribution is the contract, not a suggestion
            continue
        obs = graph.add_object("observation", {
            "text": f.text.strip(),
            "confidence": 0.8,
            "source_ids": [fetched[u] for u in urls],
            "category": "fact",
            "metadata": {"lab": "research_finding", "task_id": task_id,
                         "lab_branch_id": branch_id, "source_urls": urls,
                         "seam_versions": stamp},
        })
        if branch_id:
            graph.add_relation(branch_id, obs.id, "supported_by")
        written += 1

    if not written:
        _fail_task(graph, task_id,
                   f"research_worker: synthesis returned {dropped} finding(s) "
                   "but none carried a valid fetched-source attribution")
        return

    summary = (out.summary or "").strip()[:500]
    evaluation = graph.add_object("evaluation", {
        "subject_id": task_id,
        "subject_type": "task",
        "judgment": "research_synthesized",
        "rationale": (summary +
                      (f" ({dropped} unattributable finding(s) dropped.)"
                       if dropped else "")),
        "evaluator": "lab.research_worker",
        "metadata": {"lab": "research_synthesis", "task_id": task_id,
                     "lab_branch_id": branch_id,
                     "findings_written": written,
                     "findings_dropped": dropped,
                     "seam_versions": stamp},
    })
    if branch_id:
        graph.add_relation(branch_id, evaluation.id, "supported_by")

    task = graph.get_object(task_id)
    t_meta = dict((task.data.get("metadata") if task else {}) or {})
    t_meta["result_summary"] = summary
    graph.patch_object(task_id, {"status": "done", "metadata": t_meta})


RESEARCH_BEHAVIORS = [research_intake, research_worker]
