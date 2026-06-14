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

from .llm import AuthoredDiff, CodeOutcome, consume_llm_anomalies, is_inert
from .repo_sandbox import clone_and_read, evidence_summary, run_repo_task
from .seams import effective_setting, seam_versions_stamp
from .settings import LabSettings

_PROMPTS = {p.name: p.body for p in
            load_prompts_from_dir(Path(__file__).parent / "prompts")}
_PROMPT = _PROMPTS["code_worker"]
_AUTHOR_PROMPT = _PROMPTS["code_author"]

# A path-like token in a brief (something with a file extension): the
# diff-authoring step reads these first as the "relevant files" (ADR-037).
_PATH_RE = re.compile(r"[\w][\w./\-]*\.[A-Za-z0-9]{1,8}")

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
_AUTHORED: set[str] = set()         # authoring-request obs ids already handled
_REPO_SOURCES: dict[str, str] = {}  # repo → source object id (evidence anchor)


def clear_code_worker_registry() -> None:
    _CLAIMED.clear()
    _SYNTHESIZED.clear()
    _AUTHORED.clear()
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
            # The post-fix file states a proven diff produced (ADR-036): the
            # concrete contents an approved submit_pr commits. Never a secret —
            # the sandbox env is credential-free by construction.
            "changed_files": result.get("changed_files") or None,
            "run_error": result.get("error")}


def _task_title(graph, task_id: str) -> str:
    t = graph.get_object(task_id)
    return (t.data.get("title") if t else None) or "code task"


def _emit_synthesis_request(graph, *, title, task_id, branch_id, repo, command,
                            result, code_run_obs_id, diff, propose_pr,
                            brief) -> None:
    """Emit the ONE code-synthesis request the code_worker llm stage reacts to:
    the captured run rides whole in metadata AND the deciding exit code is in
    the text the model's view serializes. Shared by the plain/apply path
    (code_intake) and the authoring path (code_author) so the gated-submit_pr
    last mile (ADR-036) is reached identically either way."""
    graph.add_object("observation", {
        "text": (f"Code-run synthesis request for task '{title}': summarize "
                 f"what the sandbox run of '{command}' against {repo} shows. "
                 + evidence_summary(result)),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "code_synthesis_request", "task_id": task_id,
                     "lab_branch_id": branch_id, "code_run_obs": code_run_obs_id,
                     "repo": repo, "proven": bool(result.get("proven")),
                     "diff": diff if (diff and propose_pr) else None,
                     "propose_pr": bool(propose_pr), "brief": brief,
                     "run": _run_metadata(result)},
    })


def _hint_paths(task_data: dict, brief: str) -> list[str]:
    """The files the authoring step reads first: any the brief names, plus an
    explicit code_task.files hint. Best-effort — clone_and_read fills the rest
    of the budget from the tree."""
    out: list[str] = []
    meta = task_data.get("metadata") or {}
    spec = (meta.get("code_task") or {})
    for p in (spec.get("files") or []):
        if isinstance(p, str) and p not in out:
            out.append(p)
    for m in _PATH_RE.findall(brief or ""):
        if m not in out:
            out.append(m)
    return out[:12]


def _authoring_request_text(brief, command, repo, files, tree, attempt,
                            max_attempts, prev_diff, prev_failure) -> str:
    """The human/model-visible authoring request: brief, the relevant files,
    the proof command, and (on a retry) the previous attempt + its failure."""
    parts = [
        f"Code-authoring request (attempt {attempt}/{max_attempts}): author a "
        f"unified diff that fixes the defect below, to be applied and proved by "
        f"running '{command}' against {repo}.",
        f"\nBRIEF:\n{brief}",
    ]
    if files:
        parts.append("\nRELEVANT FILES:")
        for path, content in list(files.items())[:24]:
            parts.append(f"\n--- {path} ---\n{(content or '')[:6000]}")
    elif tree:
        parts.append("\nREPO FILES (no file body read):\n"
                     + ", ".join(tree[:60]))
    if prev_diff:
        parts.append("\nPREVIOUS ATTEMPT (did NOT pass — revise, do not "
                     f"repeat):\n{prev_diff[:4000]}")
    if prev_failure:
        parts.append(f"\nPREVIOUS FAILURE OUTPUT:\n{prev_failure[:4000]}")
    return "\n".join(parts)


def _emit_authoring_request(graph, settings, *, task_data, task_id, branch_id,
                            repo, command, ref, brief, attempt: int = 1,
                            prev_diff=None, prev_failure=None,
                            files_context=None, tree=None) -> None:
    """Emit a code_authoring_request the code_author llm stage reacts to
    (ADR-037). On attempt 1 the relevant files are read from a clone (no
    command run, so no run budget spent); a retry forwards the same file
    context plus the previous diff + failure so the model can revise."""
    max_attempts = max(1, int(getattr(settings, "code_author_max_attempts", 2)))
    if files_context is None:
        ctx = clone_and_read(repo, ref=ref,
                             hint_paths=_hint_paths(task_data, brief))
        if ctx.get("error"):
            _fail_task(graph, task_id,
                       f"code_worker: could not read {repo} to author a fix: "
                       f"{ctx['error']}")
            return
        files_context = ctx.get("files") or {}
        tree = ctx.get("tree") or []
    tree = tree or []
    graph.add_object("observation", {
        "text": _authoring_request_text(brief, command, repo, files_context,
                                        tree, attempt, max_attempts, prev_diff,
                                        prev_failure),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "code_authoring_request", "task_id": task_id,
                     "lab_branch_id": branch_id, "repo": repo, "ref": ref,
                     "command": command, "brief": brief, "attempt": attempt,
                     "max_attempts": max_attempts,
                     "files_context": files_context, "tree": tree[:200],
                     "prev_diff": prev_diff, "prev_failure": prev_failure},
    })


def _failure_excerpt(result: dict) -> str:
    """A short, secret-free excerpt of why an authored diff did not pass: the
    apply/clone error, else the deciding run's stderr/stdout tail."""
    if result.get("error"):
        return f"sandbox error: {result['error']}"
    run = result.get("after_diff") or result.get("baseline") or {}
    tail = (run.get("stderr") or "").strip() or (run.get("stdout") or "").strip()
    code = run.get("exit_code")
    return f"command exit={code}: {tail[-1200:]}" if tail else f"command exit={code}"


def _record_authoring_eval(graph, task_id, branch_id, repo, rationale, *,
                           authored: bool):
    """Honest verdict for an authoring run that did not land a PR (ADR-037):
    an evaluation linked to the branch — 'authored a diff but could not make it
    pass', or 'authoring could not run'. interpret fires on the task outcome."""
    ev = graph.add_object("evaluation", {
        "subject_id": task_id,
        "subject_type": "task",
        "judgment": "authoring_unproven",
        "rationale": (rationale or "")[:600],
        "evaluator": "lab.code_author",
        "metadata": {"lab": "code_authoring_failed", "task_id": task_id,
                     "lab_branch_id": branch_id, "repo": repo,
                     "authored": authored, "proven": False},
    })
    if branch_id:
        graph.add_relation(branch_id, ev.id, "supported_by")
    return ev


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
    # ADR-036: a self-repair / operator-fix task asks the loop to LAND the
    # proven fix as a PENDING (human-gated) submit_pr — carried verbatim so
    # the synthesis stage knows to open one when the diff proves.
    propose_pr = bool(spec.get("propose_pr"))
    # The prose bug description the PROPOSER attached (ADR-036): the authoring
    # brief for the diff-authoring step, falling back to the intent so a task
    # dispatched without a structured brief still carries one.
    brief = (spec.get("brief") or data.get("description")
             or data.get("title") or "")

    # Graceful degradation (ADR-036 hardening): a code task that reaches the
    # worker with a repo resolved but NO command must NOT die with "no command
    # could be resolved" (the branch#935 production failure). When the repo is
    # the lab's OWN allowlisted repo and the intent is genuine code-write work
    # (NOT a read-to-verify misroute — the branch#847 net below still owns
    # that), default the command to the lab's own proof harness (the suite
    # runner). The PROPOSER populating task.metadata.code_task is the real fix;
    # this is defense in depth for a bare-repo task that slips through (e.g. a
    # proposal an older deploy authored as a prose intent with no command).
    command_inferred = False
    if repo and not command:
        from .github_read import is_own_repo
        if is_own_repo(repo):
            from .behaviors import (_routing_for_intent,
                                    _SELF_REPAIR_DEFAULT_COMMAND)
            intent_text = data.get("description") or data.get("title") or ""
            correct = _routing_for_intent(intent_text)
            if (correct["domain"], correct["capability"]) == \
                    ("codebase", "code_task"):
                command = _SELF_REPAIR_DEFAULT_COMMAND
                command_inferred = True
    run_cap = max(1, int(effective_setting(graph, settings, "code_run_cap")))
    timeout = max(5, int(effective_setting(graph, settings,
                                           "sandbox_timeout_seconds")))

    # ADR-037, the last mile: a fix the lab was asked to LAND (propose_pr) that
    # carries a BRIEF but NO candidate diff is an AUTHORING task — the worker
    # must WRITE the fix, not just run a command. (A task that already carries a
    # candidate diff keeps the ADR-036 apply-and-prove path; a plain run task
    # with no propose_pr just runs the command.) Decided once repo + command are
    # resolved below; surfaced on the claim observation here.
    authoring = bool(propose_pr and brief and not diff and repo and command)

    # The claim observation is the graph-visible reaction; the dispatch gap
    # check reads the claim REGISTRY (task_claimed) — core's `executes`
    # relation is action→task by schema, not ours to bend (research-worker note).
    graph.add_object("observation", {
        "text": (f"Code worker claimed task '{data.get('title')}': "
                 + (("authoring a fix diff for, then proving, "
                     f"'{command}' against {repo}" if authoring else
                     f"sandbox-running '{command}' against {repo}")
                    + (" (default proof command inferred)"
                       if command_inferred else "")
                    + (" (with a proposed fix diff)" if diff else "")
                    if repo and command else
                    "no repo/command resolved — cannot run")
                 + "."),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "code_progress", "task_id": obj_id,
                     "lab_branch_id": branch_id, "repo": repo,
                     "has_diff": bool(diff), "run_cap": run_cap,
                     "command_inferred": command_inferred,
                     "authoring": authoring},
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

    if authoring:
        # Hand off to the diff-authoring stage (ADR-037): read the relevant
        # files from the clone and emit a code_authoring_request; code_author
        # writes the diff, applies + proves it in the sandbox, retries up to the
        # bound on failure, and on a PROVEN fix opens the gated submit_pr.
        _emit_authoring_request(graph, settings, task_data=data, task_id=obj_id,
                                branch_id=branch_id, repo=repo, command=command,
                                ref=ref, brief=brief)
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

    # Emit the synthesis request (ADR-036: the candidate diff + propose_pr ride
    # to the synthesis stage, which opens a gated submit_pr ONLY when the fix
    # proves; the prose brief rides too).
    _emit_synthesis_request(graph, title=data.get("title"), task_id=obj_id,
                            branch_id=branch_id, repo=repo, command=command,
                            result=result, code_run_obs_id=obs.id,
                            diff=diff if (diff and propose_pr) else None,
                            propose_pr=propose_pr, brief=brief)


@llm_behavior(
    name="code_worker",
    on=["object.created"],
    where={"object.type": "observation",
           "object.data.metadata.lab": "code_synthesis_request"},
    description=_PROMPT,
    output_schema=CodeOutcome,
    model=None,  # routes through setting.model.code_worker (ADR-019/035)
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    # artifact + decision: a PROVEN self-repair/operator-fix opens a gated
    # submit_pr (a code_change artifact + a pending submit_pr decision) — ADR-036.
    creates=["observation", "evaluation", "artifact", "decision"],
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
        # ADR-036, the self-repair loop's last mile: a PROVEN fix the lab was
        # asked to LAND (propose_pr) becomes a PENDING, human-gated submit_pr —
        # the diff + the post-fix file states the sandbox produced, citing this
        # run's sandbox_proven evaluation as the proof. The submit_pr stays
        # doubly-gated and MCP-excluded (ADR-035): opening the PR is still the
        # operator's tap; the lab only proposes. A fix that did NOT prove never
        # reaches here (the else branch records the failure and opens nothing).
        _maybe_propose_pr(graph, meta, evaluation.id, summary, stamp)
    else:
        _fail_task(graph, task_id,
                   "code_worker: sandbox run did not pass — " +
                   (summary or "the deciding command did not exit 0"))


def _maybe_propose_pr(graph, req_meta: dict, proof_eval_id: str,
                      summary: str, stamp) -> None:
    """Open a gated submit_pr for a PROVEN self-repair / operator-fix diff
    (ADR-036). Guarded: only when propose_pr was asked, a diff proved, and the
    target is the lab's OWN allowlisted repo — self-dispatch never opens a PR
    against anything but the lab's own code (the Phase-3 guardrail). The write
    token is NOT touched here; propose_submit_pr_fn only opens the PENDING
    decision the operator must approve."""
    from .github_read import is_own_repo
    repo = req_meta.get("repo")
    diff = req_meta.get("diff")
    if not (req_meta.get("propose_pr") and diff and repo):
        return
    if not is_own_repo(repo):
        # A self-proposed fix may target ONLY the lab's own repo. (Should never
        # reach here — self_repair bounds the proposal — but the loop's PR mouth
        # enforces it independently: defense in depth, ADR-036.)
        graph.add_object("observation", {
            "text": (f"Self-repair PR NOT proposed: the proven fix targets "
                     f"'{repo}', which is not the lab's own repo. Self-"
                     f"dispatched repair is bounded to the lab's own "
                     f"allowlisted repo (ADR-036)."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "self_repair_blocked", "repo": repo,
                         "lab_branch_id": req_meta.get("lab_branch_id")},
        })
        return
    branch_id = req_meta.get("lab_branch_id")
    run = req_meta.get("run") or {}
    files = (run.get("changed_files") or {})
    head_branch = f"lab/self-repair-{str(branch_id or 'fix').replace('#', '-')}"
    title = (f"Self-repair: {summary}" if summary else "Self-repair fix")[:120]
    from .tools import propose_submit_pr_fn
    artifact, decision = propose_submit_pr_fn(
        graph, repo, head_branch=head_branch, title=title, diff=diff,
        files=files, branch_id=branch_id, proof_refs=[proof_eval_id],
        rationale=(
            f"Open a pull request against {repo} for the fix the sandbox "
            f"proved (see the cited sandbox_proven evaluation). The lab "
            f"self-dispatched this repair from its own observed defect; the "
            f"diff is proven green in the sandbox. On approval the lab pushes "
            f"the branch and opens the PR; the operator reviews and merges on "
            f"GitHub — two human gates, no auto-merge. Rejection touches no "
            f"write token."))
    obs = graph.add_object("observation", {
        "text": (f"Self-repair fix proven in the sandbox — opened a PENDING "
                 f"submit_pr decision ({decision.id}) for {repo}. The operator "
                 f"approves (or rejects) it from the inbox; no PR exists yet."),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "self_repair_pr_proposed", "repo": repo,
                     "lab_branch_id": branch_id, "decision_id": decision.id,
                     "artifact_id": artifact.id, "proof_eval": proof_eval_id,
                     "seam_versions": stamp},
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")


@llm_behavior(
    name="code_author",
    on=["object.created"],
    where={"object.type": "observation",
           "object.data.metadata.lab": "code_authoring_request"},
    description=_AUTHOR_PROMPT,
    output_schema=AuthoredDiff,
    model=None,  # routes through setting.model.code_worker (ADR-019/037 —
                 # authoring a fix is top-tier reasoning, same plane as synthesis)
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["observation", "evaluation"],
    max_tokens=4096,
    tools=[],
)
def code_author(event, graph, ctx, out, *, settings: LabSettings):
    """Author a unified diff for a fix, apply + prove it in the sandbox, retry
    up to the bound, and on a PROVEN fix hand off to the synthesis stage which
    opens the gated submit_pr (ADR-037 — the self-repair loop's last mile).

    On: object.created (observation, metadata.lab code_authoring_request).
    The LLM authors a diff from the brief + the relevant files (read from the
    clone by code_intake). The lab applies it and runs the proof command — the
    RUN decides success, never the model. Tests pass → emit the synthesis
    request (the existing propose_pr → submit_pr path). Tests fail → feed the
    failure back and retry, up to setting.code_author_max_attempts; still
    failing → an honest 'authored a diff but could not make it pass'
    evaluation, the task rejected, NO submit_pr (a fix must earn its PR).
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    req_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    if meta.get("lab") != "code_authoring_request" or not req_id:
        return
    if req_id in _AUTHORED:
        return
    _AUTHORED.add(req_id)
    if not settings.code_worker_enabled:
        return

    task_id = meta.get("task_id")
    branch_id = meta.get("lab_branch_id")
    repo, command, ref = meta.get("repo"), meta.get("command"), meta.get("ref")
    brief = meta.get("brief") or ""
    attempt = int(meta.get("attempt") or 1)
    max_attempts = int(meta.get("max_attempts") or 1)
    if not task_id:
        return

    diff = ((getattr(out, "diff", None) or "").strip()) if out is not None else ""
    if out is None or is_inert(getattr(out, "notes", None)) or not diff:
        # Inert/empty authoring output (LLM budget, pause, parse) — no diff was
        # produced. Do NOT burn a retry on a non-authoring failure; record the
        # honest gap and fail the task (the preceding anomaly observation has
        # the cause).
        _record_authoring_eval(
            graph, task_id, branch_id, repo,
            (f"Diff authoring produced no usable output on attempt {attempt} "
             f"(LLM budget, pause, or parse failure). No diff authored; opening "
             f"no PR."), authored=False)
        _fail_task(graph, task_id,
                   "code_worker: diff authoring produced no usable output (LLM "
                   "budget, pause, or parse failure)")
        return

    run_cap = max(1, int(effective_setting(graph, settings, "code_run_cap")))
    timeout = max(5, int(effective_setting(graph, settings,
                                           "sandbox_timeout_seconds")))
    # Apply the authored diff and run the proof command (the apply + re-run is 2
    # runs; with run_cap < 2 we cannot prove an authored diff, so fail honestly).
    if run_cap < 2:
        _record_authoring_eval(
            graph, task_id, branch_id, repo,
            "Authored a diff but code_run_cap < 2 leaves no room to apply and "
            "re-run to prove it; opening no PR.", authored=True)
        _fail_task(graph, task_id,
                   "code_worker: code_run_cap < 2 — cannot prove an authored diff")
        return
    result = run_repo_task(repo, command, ref=ref, diff=diff,
                           timeout_seconds=timeout)

    source_id = _ensure_repo_source(graph, repo, ref)
    stamp = seam_versions_stamp(graph, "prompt.code_author")
    run_obs = graph.add_object("observation", {
        "text": (f"Authored a fix diff (attempt {attempt}/{max_attempts}) and "
                 f"applied it in the sandbox. " + evidence_summary(result)),
        "confidence": 0.9,
        "source_ids": [source_id],
        "category": "measurement",
        "metadata": {"lab": "code_authored_run", "task_id": task_id,
                     "lab_branch_id": branch_id, "source_id": source_id,
                     "attempt": attempt, "max_attempts": max_attempts,
                     "authored_diff": diff,
                     "authoring_notes": (getattr(out, "notes", "") or "")[:500],
                     "seam_versions": stamp, "run": _run_metadata(result)},
    })
    if branch_id:
        graph.add_relation(branch_id, run_obs.id, "supported_by")

    if result.get("proven"):
        # The authored fix is proven in the sandbox. Hand the diff + the proven
        # run (with the post-fix file states the PR commits) to the synthesis
        # stage, which writes the honest summary, the sandbox_proven evaluation,
        # completes the task, and opens the gated submit_pr — ADR-037 reuses
        # ADR-035's last mile rather than duplicating it.
        _emit_synthesis_request(
            graph, title=_task_title(graph, task_id), task_id=task_id,
            branch_id=branch_id, repo=repo, command=command, result=result,
            code_run_obs_id=run_obs.id, diff=diff, propose_pr=True, brief=brief)
        return

    failure = _failure_excerpt(result)
    if attempt < max_attempts:
        # Feed the failure back and retry — the same file context, the previous
        # diff, and the captured failure so the model can revise (bounded).
        _emit_authoring_request(
            graph, settings,
            task_data={"title": _task_title(graph, task_id),
                       "description": brief}, task_id=task_id,
            branch_id=branch_id, repo=repo, command=command, ref=ref,
            brief=brief, attempt=attempt + 1, prev_diff=diff,
            prev_failure=failure, files_context=meta.get("files_context") or {},
            tree=meta.get("tree") or [])
        return

    # Bounded out: honest gap. The lab DID author a fix — it just could not make
    # it pass. No submit_pr (a fix must earn its PR by proving in the sandbox).
    _record_authoring_eval(
        graph, task_id, branch_id, repo,
        (f"Authored a diff but could not make it pass after {max_attempts} "
         f"attempt(s): running '{command}' against {repo} with the authored "
         f"change applied did not exit 0. {failure} No PR opened."),
        authored=True)
    _fail_task(graph, task_id,
               f"code_worker: authored a diff but could not make it pass after "
               f"{max_attempts} attempt(s) — opening no PR")


CODE_BEHAVIORS = [code_intake, code_worker, code_author]
