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

# ADR-022/020: file CONTENTS, not just the tree. When a task is directed at a
# GitHub repo, the bare-repo URL routes to get_tree (recursive); the worker
# then picks RELEVANT implementation files from the returned tree and fetches
# their contents as further tool_gateway calls, within the per-task fetch cap.
# The files become attributed evidence exactly like any other fetched source.
#
# "Relevant" is a heuristic, justified here: keep blob entries whose extension
# is source/docs/config (skip binaries, lockfiles, vendored trees), score by
# overlap with the task's intent/direction keywords, give shallow paths and
# obvious entrypoints (README, __init__, main, kernel, pack manifests) a small
# bonus, and break ties deterministically by path so fixtures are stable.
_CODE_EXTS = {
    ".py", ".md", ".rst", ".txt", ".js", ".ts", ".tsx", ".jsx", ".mjs",
    ".go", ".rs", ".java", ".rb", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".sh", ".sql",
    ".html", ".css", ".tf", ".proto", ".swift", ".kt", ".php", ".scala",
}
_SKIP_PATH_RE = re.compile(
    r"(?:^|/)(?:node_modules|vendor|dist|build|\.git|__pycache__|"
    r"site-packages|third_party|fixtures?|tests?|test)/", re.I)
_ENTRYPOINT_RE = re.compile(
    r"(?:^|/)(?:readme|__init__|__main__|main|index|kernel|pack|setup|"
    r"app|server|cli|core)\b", re.I)
_MAX_FILE_BYTES = 400_000  # skip very large blobs; they truncate to noise
_KEYWORD_RE = re.compile(r"[a-z][a-z0-9_]{3,}")
_STOPWORDS = frozenset((
    "this", "that", "with", "from", "into", "your", "have", "using", "use",
    "verify", "research", "claim", "claims", "source", "sources", "primary",
    "evidence", "find", "produce", "test", "tests", "code", "file", "files",
    "implementation", "actual", "fetch", "repo", "repository", "github",
    "about", "their", "they", "them", "what", "when", "which", "where",
    "https", "http", "com", "www", "blob", "tree", "main", "master",
))


def _intent_keywords(text: str) -> set[str]:
    return {w for w in _KEYWORD_RE.findall((text or "").lower())
            if w not in _STOPWORDS}


def _select_repo_files(entries: list, intent_text: str, budget: int) -> list[str]:
    """Pick up to `budget` relevant implementation-file paths from a repo
    tree's entries (each {path, type, size}). Deterministic ordering."""
    if budget <= 0:
        return []
    keywords = _intent_keywords(intent_text)
    scored: list[tuple] = []
    for e in entries or []:
        if (e.get("type") or "") != "blob":
            continue
        path = e.get("path") or ""
        if not path or _SKIP_PATH_RE.search(path):
            continue
        dot = path.rfind(".")
        ext = path[dot:].lower() if dot != -1 else ""
        if ext not in _CODE_EXTS:
            continue
        size = e.get("size")
        if isinstance(size, int) and size > _MAX_FILE_BYTES:
            continue
        low = path.lower()
        score = sum(2 for kw in keywords if kw in low)
        if _ENTRYPOINT_RE.search(low):
            score += 1
        depth = path.count("/")
        # Higher score first; then shallower; then alphabetical path. Negate
        # for ascending sort, path last so ties are fully deterministic.
        scored.append((-score, depth, path))
    scored.sort()
    return [p for _s, _d, p in scored[:budget]]

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


# The concrete tools the research lane provides when it is live: source
# retrieval through tool_gateway (web fetch) and the read-only GitHub tools
# (ADR-022) — get_file among them, shipped in ADR-028. The capability
# self-check (ADR-031) and the phantom-work guard (ADR-032) consult this so no
# behavior claims the lab "lacks the means" to retrieve file contents while
# this worker is live. Mirrors the github capability_provider registered in
# _ensure_github_provider plus web.fetch_url.
RESEARCH_WORKER_TOOLS = frozenset({
    "web.fetch_url",
    "github.get_tree", "github.get_file", "github.list_commits",
    "github.list_issues", "github.list_pulls",
})


def research_lane_available(settings) -> bool:
    """Is the research lane live? It is the lab's reactor for
    research.deep_research and carries get_file et al. (ADR-022/028). A
    capability is only 'available' when its reactor is enabled — disabled, the
    capability-gap path is the honest answer (ADR-031)."""
    return bool(getattr(settings, "research_worker_enabled", False))


def task_claimed(task_id: str) -> bool:
    """The dispatch gap check consults this: a claim IS a reaction."""
    return task_id in _CLAIMED


def _clean_url(url: str) -> str:
    return url.split("#")[0].rstrip("/").rstrip(".,;")


def _source_urls(graph, task_data: dict, cap: int) -> list[str]:
    """Candidate sources for a research task, deduped and capped: the
    branch's claim observation URL, the mission's target URL, and any URLs
    written into the task description / claim text (the operator can steer
    sources by mentioning links). An operator_direction on the task
    (ADR-027) is scanned the same way — direction URLs come FIRST: the
    operator named them deliberately, so the fetch cap must not starve them
    behind the defaults."""
    meta = task_data.get("metadata") or {}
    direction = (meta.get("operator_direction") or "").strip()
    candidates: list[str] = _URL_RE.findall(direction) if direction else []
    # Operator-supplied URLs (task description + activation_message) come
    # ahead of the derived claim/mission defaults: the operator named these
    # deliberately, so the fetch cap must not starve them behind defaults.
    candidates.extend(_URL_RE.findall(task_data.get("description") or ""))
    candidates.extend(_URL_RE.findall(meta.get("activation_message") or ""))
    branch = graph.get_object(meta.get("lab_branch_id")) \
        if meta.get("lab_branch_id") else None
    if branch is not None:
        b_meta = branch.data.get("metadata") or {}
        claim = graph.get_object(b_meta.get("claim_observation_id")) \
            if b_meta.get("claim_observation_id") else None
        if claim is not None:
            candidates.extend(_URL_RE.findall(claim.data.get("text") or ""))
            c_url = (claim.data.get("metadata") or {}).get("url")
            if c_url:
                candidates.append(c_url)
        mission = graph.get_object(branch.data.get("mission_id")) \
            if branch.data.get("mission_id") else None
        if mission is not None and (mission.data.get("target_url") or "").strip():
            candidates.append(mission.data["target_url"].strip())
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


def _enqueue_file_fetch(graph, task_id: str, state: dict, repo: str,
                        path: str, ref) -> None:
    """Propose one github.get_file tool_gateway call and register it against
    the task (counts toward the fetch cap)."""
    input_data = {"repo": repo, "path": path}
    if ref:
        input_data["ref"] = ref
    url = f"https://github.com/{repo}/blob/{ref or 'HEAD'}/{path}"
    from .behaviors import _now
    call = graph.add_object("capability_call", {
        "provider_id": _ensure_github_provider(graph),
        "provider_name": "github",
        "capability_name": "get_file",
        "input_data": input_data,
        "risk_class": "low",
        "status": "proposed",
        "proposed_by": "lab.research_worker",
        "proposed_at": _now(),
        "metadata": {"lab_research": True, "task_id": task_id, "url": url,
                     "from_tree": True},
    })
    _CALLS[call.id] = task_id
    state["pending"].add(call.id)
    state["issued"] += 1


def _maybe_expand_repo_tree(graph, task_id: str, state: dict, call_id: str,
                            tree_text: str) -> None:
    """If a just-returned fetch was a github get_tree, pick relevant files and
    enqueue get_file fetches for them within the remaining fetch-cap budget
    (ADR-022/020). Only the first tree per task expands — the cap is a budget,
    not a per-tree allowance."""
    if state.get("tree_expanded"):
        return
    call = graph.get_object(call_id)
    if call is None:
        return
    cdata = call.data
    if cdata.get("provider_name") != "github" or \
            cdata.get("capability_name") != "get_tree":
        return
    state["tree_expanded"] = True
    budget = state["cap"] - state["issued"]
    if budget <= 0:
        return
    try:
        tree = json.loads(tree_text)
        entries = tree.get("entries") or []
    except (ValueError, AttributeError):
        return  # a truncated/garbled tree degrades to no expansion, never a crash
    repo = (cdata.get("input_data") or {}).get("repo") or tree.get("repo")
    ref = (cdata.get("input_data") or {}).get("ref") or tree.get("ref")
    if not repo:
        return
    for path in _select_repo_files(entries, state.get("intent") or "", budget):
        _enqueue_file_fetch(graph, task_id, state, repo, path, ref)


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
                 "branch_id": branch_id, "cap": cap,
                 # Total tool_gateway calls issued for this task (initial
                 # sources + any tree-expansion file fetches): the per-task
                 # fetch cap binds on ALL of them, not just the first round.
                 "issued": len(urls),
                 "tree_expanded": False,
                 # The task intent steers which repo files are relevant.
                 "intent": ((data.get("description") or "") + " "
                            + (meta.get("operator_direction") or "")),
                 # ADR-027: the operator's continuation direction rides from
                 # the dispatched task into the synthesis request VERBATIM.
                 "direction": (meta.get("operator_direction") or "").strip()}
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
        state["failed"].append({"url": url, 
…[truncated, 26366 chars total]