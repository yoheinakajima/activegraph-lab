"""Lab-local code worker — PLUMBING (ADR-035, rung 2 of ADR-022).

Mirrors the research worker (ADR-020): implements the EXISTING routing
contract (ADR-006) by reacting to core task objects routed
codebase.code_task — the lane the work behavior already emits for code-write
intent and that has had NO reactor until now (every code_task recorded a
capability gap). Dispatch code is untouched; the worker is droppable —
disabled (`code_worker_enabled` False, the default) or deleted, the
capability-gap path takes over again.

Two reactive stages over one log, no orchestrator — the research-worker shape:

  code_intake  — task.created (routing codebase.code_task) → claim (a
                 code_progress observation + a registry entry the dispatch
                 gap check consults) → resolve the code-task spec (repo,
                 command, optional ref, optional fix diff) → run it in the
                 repo sandbox (clone an ALLOWLISTED repo, run the command;
                 for a fix-task apply the diff and re-run to PROVE it) →
                 record the captured output as ATTRIBUTED evidence (a
                 code_run observation linked to a repo source) → emit ONE
                 code-synthesis request. A missing/non-allowlisted repo or
                 absent command fails the task with the error IN the event —
                 errors propagate, never a silent abort.
  code_worker  — llm_behavior on the synthesis request: write a short, honest
                 summary of what the run shows (grounded in the captured exit
                 codes/output), an evaluation linked supported_by to the
                 branch, and COMPLETE the task — done if the deciding run
                 exited 0, failed otherwise (the verdict is the run's, not the
                 model's). Inert LLM output (budget, pause, parse) fails the
                 task with the reason recorded.

The sandbox is the isolation foundation (lab_pack/repo_sandbox.py): clean env
(no inherited secrets), wall-clock + resource bounds, allowlisted repos only,
subprocess-hardened. Run cap per task is setting.code_run_cap; the per-run
wall-clock budget is setting.sandbox_timeout_seconds. The LLM stage rides the
LabProviderWrapper like every lab llm_behavior — pause and the daily cost cap
gate it there (the same rails the research worker's synthesis stage runs on);
the model routes through setting.model.code_worker (default claude-opus-4-8,
ADR-019/035 — reasoning over a change and its test output is top-tier work).

Enablement: code_worker_enabled defaults False so no fixture or embedding
clones/runs by surprise; the server boot enables it. Resume: claimed task ids
rebuild from code_progress observations (server._rebuild_lab_registries).
"""

from __future__ import annotations

import re
from pathlib import Path

from activegraph.packs import behavior, llm_behavior, load_prompts_from_dir

from .llm import CodeOutcome, consume_llm_anomalies, is_inert
from .repo_sandbox import evidence_summary, run_repo_task
from .seams import effective_setting, seam_versions_stamp
from .settings import LabSettings

_PROMPT = next(p.body for p in
               load_prompts_from_dir(Path(__file__).parent / "prompts")
               if p.name == "code_worker")

# github.com/<owner>/<repo>[...] → owner/repo (drop a trailing .git / path)
_REPO_RE = re.compile(r"github\.com[/:]([\w.\-]+/[\w.\-]+?)(?:\.git)?(?:[/#?\s]|$)")
# A fenced or inline `command: …` the operator can put in an intent/direction.
_COMMAND_RE = re.compile(r"(?:^|\n)\s*command:\s*(.+)", re.I)

# The concrete tool the code lane provides when it is live: the repo sandbox
# (clone an allowlisted repo + run a command). The capability self-check
# (ADR-031) consults this so no behavior asserts the lab "cannot run code"
# while this worker is live. Kept separate from RESEARCH_WORKER_TOOLS — the
# phantom-work alias map (ADR-032) is keyed to the research read tools; the
# sandbox-build proposal is left deliberately UNALIASED (a genuine "build a
# sandbox" follow-up is still allowed to propose; see ADR-035).
CODE_WORKER_TOOLS = frozenset({"sandbox.clone_and_run"})


def code_lane_available(settings) -> bool:
    """Is the code lane live? It is the lab's reactor for codebase.code_task
    and carries the repo sandbox. A capability is only 'available' when its
    reactor is enabled — disabled, the capability-gap path is the honest
    answer (ADR-031)."""
    return bool(getattr(settings, "code_worker_enabled", False))


# ── registries (caches + dedup; rebuilt on resume, never the truth) ─────────
_CLAIMED: set[str] = set()          # task ids this worker claimed
_SYNTHESIZED: set[str] = set()      # task ids the llm stage already handled
_REPO_SOURCES: dict[str, str] = {}  # repo → source object id (evidence anchor)


def clear_code_worker_registry() -> None:
    _CLAIMED.clear()
    _SYNTHESIZED.clear()
    _REPO_SOURCES.clear()


def task_claimed(task_id: str) -> bool:
    """The dispatch gap check consults this: a claim IS a reaction."""
    return task_id in _CLAIMED


def _resolve_spec(graph, task_data: dict) -> dict:
    """The code-task spec for a routed task: an explicit
    task.metadata.code_task (repo, command, ref, diff) wins; otherwise parse
    a github repo URL and a `command:` line out of the intent / the operator
    direction (the mentioning-links principle, ADR-027, extended to code)."""
    meta = task_data.get("metadata") or {}
    spec = dict(meta.get("code_task") or {})
    text = " ".join([
        task_data.get("description") or "",
        meta.get("operator_direction") or "",
        meta.get("activation_message") or "",
    ])
    if not spec.get("repo"):
        m = _REPO_RE.search(text)
        if m:
            spec["repo"] = m.group(1)
    if not spec.get("command"):
        m = _COMMAND_RE.search(text)
        if m:
            spec["command"] = m.group(1).strip()
    return spec


def _ensure_repo_source(graph, repo: str, ref) -> str:
    """Idempotently create a core source for the repo — the attribution anchor
    every code_run observation links to (evidence has a provenance, ADR-020)."""
    if repo in _REPO_SOURCES:
        return _REPO_SOURCES[repo]
    src = graph.add_object("source", {
        "kind": "repo",
        "url": f"https://github.com/{repo}",
        "content": f"Sandbox clone of {repo}"
                   + (f" @ {ref}" if ref else "") + ".",
        "channel": "lab",
        "metadata": {"lab": "code_repo_source", "repo": repo, "ref": ref},
    })
    _REPO_SOURCES[repo] = src.id
    return src.id


def _fail_task(graph, task_id: str, error: str) -> None:
    """Task failed, error recorded IN the event (the patch diff carries it) —
    errors propagate; the worker never silently aborts (mirrors research)."""
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = dict(task.data.get("metadata") or {})
    meta["error"] = error[:500]
    meta["result_summary"] = error[:500]
    graph.patch_object(task_id, {"status": "rejected", "metadata": meta})


def _run_metadata(result: dict) -> dict:
    """The captured run, secret-free, for the code_run observation's metadata.
    The sandbox already strips inherited secrets from the child env and
    truncates each stream, so the captured output carries no credential by
    construction (Phase-1 sentinel-gated)."""
    def _compact(run):
        if not run:
            return None
        return {"command": run.get("command"), "exit_code": run.get("exit_code"),
                "timed_out": run.get("timed_out"),
                "duration_seconds": run.get("duration_seconds"),
                "error": run.get("error"),
                "stdout": run.get("stdout") or "",
                "stderr": run.get("stderr") or ""}
    return {"repo": result.get("repo"), "ref": result.get("ref"),
            "baseline": _compact(result.get("baseline")),
            "after_diff": _compact(result.get("after_diff")),
            "proven": bool(result.get("proven")),
            "run_error": result.get("error")}


@behavior(
    name="code_intake",
    on=["object.created"],
    creates=["source", "observation"],
)
def code_intake(event, graph, ctx, *, settings: LabSettings):
    """Claim code-routed tasks, run them in the repo sandbox, record evidence.

    On: object.created (task, metadata.routing codebase.code_task) — claim,
        resolve the spec, run the sandbox, write a code_run observation, and
        emit ONE code-synthesis request (or fail the task with the error when
        the repo/command is missing or the sandbox could not run).
    """
    if not settings.code_worker_enabled:
        return
    obj = event.payload.get("object", {})
    obj_id = obj.get("id")
    if obj.get("type") != "task":
        return
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    routing = meta.get("routing") or {}
    if (routing.get("domain"), routing.get("capability")) != \
            ("codebase", "code_task"):
        return  # not ours — the capability-gap path stays untouched
    if not meta.get("lab_branch_id") or obj_id in _CLAIMED:
        return
    _CLAIMED.add(obj_id)
    branch_id = meta.get("lab_branch_id")

    spec = _resolve_spec(graph, data)
    repo, command = spec.get("repo"), spec.get("command")
    ref, diff = spec.get("ref"), spec.get("diff")
    run_cap = max(1, int(effective_setting(graph, settings, "code_run_cap")))
    timeout = max(5, int(effective_setting(graph, settings,
                                           "sandbox_timeout_seconds")))

    # The claim observation is the graph-visible reaction; the dispatch gap
    # check reads the claim REGISTRY (task_claimed) — core's `executes`
    # relation is action→task by schema, not ours to bend (research-worker note).
    graph.add_object("observation", {
        "text": (f"Code worker claimed task '{data.get('title')}': "
                 + (f"sandbox-running '{command}' against {repo}"
                    + (" (with a proposed fix diff)" if diff else "")
                    if repo and command else
                    "no repo/command resolved — cannot run")
                 + "."),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "code_progress", "task_id": obj_id,
                     "lab_branch_id": branch_id, "repo": repo,
                     "has_diff": bool(diff), "run_cap": run_cap},
    })

    if not command:
        # No command means there is nothing to execute. The common cause is a
        # READ-TO-VERIFY task misrouted into the codebase lane (the branch#847
        # family — ADR-031): the ACTION is fetch/read/verify, not code-write,
        # and that work belongs to research.deep_research, which carries
        # get_file. Rather than die in a dead lane, record an honest,
        # actionable verdict naming the right lane so the operator can
        # re-dispatch verb-safely — and so interpret still fires on the outcome.
        from .behaviors import _routing_for_intent
        intent = (data.get("description") or data.get("title") or "")
        correct = _routing_for_intent(intent)
        if (correct["domain"], correct["capability"]) == ("research", "deep_research"):
            obs = graph.add_object("observation", {
                "text": (f"Misrouted, capability available: task "
                         f"'{data.get('title')}' was routed codebase.code_task "
                         f"but its action is read-to-verify, not code-write — "
                         f"that work belongs to research.deep_research, which "
                         f"carries get_file. Not reached here: a routing miss, "
                         f"not a capability absence. Re-dispatch under research "
                         f"with a verb-safe message (fetch/read/verify, no "
                         f"build/implement verb)."),
                "confidence": 0.95,
                "category": "risk",
                "metadata": {"lab": "routing_miss", "task_id": obj_id,
                             "lab_branch_id": branch_id,
                             "misrouted_from": "codebase.code_task",
                             "correct_routing": "research.deep_research"},
            })
            if branch_id:
                graph.add_relation(branch_id, obs.id, "supported_by")
            _fail_task(graph, obj_id,
                       "code_worker: this is read-to-verify work (no command to "
                       "run); research.deep_research owns it (get_file). "
                       "Re-dispatch under research.")
            return
        _fail_task(graph, obj_id,
                   "code_worker: no command could be resolved for this task "
                   "(set task.metadata.code_task = {repo, command} or name a "
                   "'command:' line in the intent)")
        return
    if not repo:
        _fail_task(graph, obj_id,
                   "code_worker: no repo could be resolved for this task "
                   "(set task.metadata.code_task.repo or name a github repo "
                   "in the intent)")
        return

    # A fix-task is baseline + re-run = 2 runs; a plain command is 1 run. The
    # run cap bounds it — diff dropped if the cap leaves no room for the re-run.
    apply_diff = diff if (diff and run_cap >= 2) else None
    result = run_repo_task(repo, command, ref=ref, diff=apply_diff,
                           timeout_seconds=timeout)

    source_id = _ensure_repo_source(graph, repo, ref)
    obs = graph.add_object("observation", {
        "text": evidence_summary(result),
        "confidence": 0.9,
        "source_ids": [source_id],
        "category": "measurement",
        "metadata": {"lab": "code_run", "task_id": obj_id,
                     "lab_branch_id": branch_id, "source_id": source_id,
                     "run": _run_metadata(result)},
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")

    if result.get("error") and result.get("baseline") is None:
        # The sandbox could not even run (refused repo, clone/apply failure) —
        # that is a task failure with the error on the record, not a synthesis.
        _fail_task(graph, obj_id, f"code_worker: {result['error']}")
        return

    # Emit the synthesis request: the captured run rides whole in metadata AND
    # the deciding exit code is in the text the model's view serializes.
    graph.add_object("observation", {
        "text": (f"Code-run synthesis request for task "
                 f"'{data.get('title')}': summarize what the sandbox run of "
                 f"'{command}' against {repo} shows. " + evidence_summary(result)),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "code_synthesis_request", "task_id": obj_id,
                     "lab_branch_id": branch_id, "code_run_obs": obs.id,
                     "repo": repo, "proven": bool(result.get("proven")),
                     "run": _run_metadata(result)},
    })


@llm_behavior(
    name="code_worker",
    on=["object.created"],
    where={"object.type": "observation",
           "object.data.metadata.lab": "code_synthesis_request"},
    description=_PROMPT,
    output_schema=CodeOutcome,
    model=None,  # routes through setting.model.code_worker (ADR-019/035)
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["observation", "evaluation"],
    max_tokens=1024,
    tools=[],
)
def code_worker(event, graph, ctx, out, *, settings: LabSettings):
    """Summarize the sandbox run into attributed evidence and complete the task.

    Creates: one summary observation (attributed to the repo source), one
    evaluation linked supported_by to the branch, and the task completion
    patch — DONE if the deciding run exited 0 (the change is proven in the
    sandbox), REJECTED otherwise (the run's verdict, not the model's). Inert
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
    if not settings.code_worker_enabled:
        return

    if out is None or is_inert(getattr(out, "summary", None)):
        _fail_task(graph, task_id,
                   "code_worker: synthesis produced no usable output (LLM "
                   "budget, pause, or parse failure — see the preceding "
                   "observation)")
        return

    proven = bool(meta.get("proven"))
    run = meta.get("run") or {}
    source_id = _REPO_SOURCES.get(meta.get("repo")) or \
        (graph.get_object(meta.get("code_run_obs")).data.get("metadata") or {}).get(
            "source_id") if meta.get("code_run_obs") else None
    summary = (out.summary or "").strip()[:600]
    stamp = seam_versions_stamp(graph, "prompt.code_worker")

    obs = graph.add_object("observation", {
        "text": summary or evidence_summary({"repo": meta.get("repo"),
                                             "baseline": run.get("baseline"),
                                             "after_diff": run.get("after_diff"),
                                             "proven": proven, "error": None}),
        "confidence": 0.85,
        "source_ids": [source_id] if source_id else [],
        "category": "measurement",
        "metadata": {"lab": "code_finding", "task_id": task_id,
                     "lab_branch_id": branch_id, "repo": meta.get("repo"),
                     "proven": proven, "seam_versions": stamp},
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")

    evaluation = graph.add_object("evaluation", {
        "subject_id": task_id,
        "subject_type": "task",
        "judgment": "sandbox_proven" if proven else "sandbox_not_proven",
        "rationale": summary or evidence_summary(
            {"repo": meta.get("repo"), "baseline": run.get("baseline"),
             "after_diff": run.get("after_diff"), "proven": proven,
             "error": None}),
        "evaluator": "lab.code_worker",
        "metadata": {"lab": "code_synthesis", "task_id": task_id,
                     "lab_branch_id": branch_id, "repo": meta.get("repo"),
                     "proven": proven, "seam_versions": stamp},
    })
    if branch_id:
        graph.add_relation(branch_id, evaluation.id, "supported_by")

    if proven:
        task = graph.get_object(task_id)
        t_meta = dict((task.data.get("metadata") if task else {}) or {})
        t_meta["result_summary"] = summary
        graph.patch_object(task_id, {"status": "done", "metadata": t_meta})
    else:
        _fail_task(graph, task_id,
                   "code_worker: sandbox run did not pass — " +
                   (summary or "the deciding command did not exit 0"))


CODE_BEHAVIORS = [code_intake, code_worker]
