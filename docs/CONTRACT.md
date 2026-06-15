# CONTRACT

Invariants. Changing any item below requires an ADR in docs/DECISIONS.md and, once the runtime exists, a decision object.

## Object types

- The lab is a layered pack on activegraph-packs core (ADR-003).
- The lab adds exactly three types: `mission`, `branch`, `decision`.
- Everything else is core: artifact for outputs, observation + evaluation for evidence, task for work dispatch, source for ingested pages/repos/papers.
- Adding a fourth lab type requires a gated decision object AND an ADR.

## Event log

- All user-visible state derives from the event log. The UI is a projection.
- No side database.
- One scoped exception (ADR-023): the `/lab/errors` diagnostics projection is a volatile in-process ring buffer — it exists precisely for the failure domain where appending to the log is the thing that broke. Memory only, lost on restart, never authoritative.

## Chat path

- In any chat path, the message append is the ONLY step whose failure may fail the request (ADR-023). Never an error after a successful append. The reply runs on the worker regardless of the client's fate. The MCP `send_chat` path is commit-and-return (ADR-034): a successful append returns `status=accepted` with the committed message ids IMMEDIATELY — no bounded wait — and the reply is read via `get_branch`; a post-commit degradation still returns `accepted`. The HTTP `POST /chat` path composes its reply inline and degrades to `reply_pending` with the committed message ids.

## Authority

- One bit per capability: `auto` or `gated`.
- Publishing and self-modification are always gated. Self-modification additionally requires its enabling flag (`LAB_ALLOW_GRAPH_CODE=1` for graph code) — an approved decision alone is not enough (ADR-012).
- Opening a pull request is always gated and doubly so (ADR-035): a `submit_pr` decision must be approved by the operator (gate one) AND the resulting PR reviewed and merged by the operator on GitHub (gate two) — no auto-merge, ever. The write-scoped `GITHUB_WRITE_TOKEN` (separate from the read `GITHUB_TOKEN`, fine-grained, lab-repo-only, absent until configured) is exercised ONLY on an approved submit_pr, rides only in a request header, and is NEVER in any event payload, observation, artifact, log line, or error — sentinel-audited like every secret. `submit_pr` is EXCLUDED from MCP exactly like approve/reject — the inbox is human-only.
- Decision resolutions record the operator's reasons on the resolution event (`resolution_rationale`, `resolved_by` — ADR-026). MCP may annotate a pending decision (public, attributed commentary) but never resolve it: approve/reject remain EXCLUDED from MCP (ADR-016/021/026).

## Code residency (ADR-012)

- KERNEL stays in git forever: gate, auth, runtime wiring, replay, storage adapter, loaders, the manifest (`lab_pack/kernel.py`). The thing that governs self-modification is never subject to it.
- SEAMS and GRAPH CODE live in the graph, gated; PLUMBING in git. Loaders refuse artifacts referencing the kernel manifest.

## Messages and steering

- A user message is an event in the branch's log (a comm_message in a thread that discusses the branch — ADR-004).
- Replies come from a fast answer behavior that reads graph state, never blocks on running work, and stamps its event horizon.
- Steering takes effect at event boundaries.
- Steering replies are truthful (ADR-025): mutations apply before the reply is composed; a reply may claim only actions whose `lab.steering_applied` event it cites; an action request no verb supports draws an explicit refusal naming the verb set.
- Chat approve/reject keys by the thread's branch and is REFUSED for MCP-tagged messages — the inbox stays human-only (ADR-016/021/025).

## Workers

- The lab never calls domain packs directly. Work is dispatched as core task objects with routing tags; packs react or a capability-gap observation is recorded (ADR-006). Tasks route by their ACTION verb (ADR-025/ADR-006): reading source to verify/check/examine a claim is `research.deep_research`; only WRITE/MODIFY/GENERATE-code intent is `codebase.code_task`. A mention of code, files, or a repo is not a request to write code. The lab-local research worker (ADR-020) and code worker (ADR-035) are droppable plumbing that REACT to these routed tasks from inside the lab, implementing the same routing contract an upstream pack would — they call no domain pack; disable or delete either and the capability-gap path takes over unchanged.
- Workers emit a progress event at least every 60 seconds, or declare the current step uninterruptible.
- All external fetches go through tool_gateway.
- Honesty about capability (ADR-031): no behavior asserts a capability is absent ("lacks the means" / "cannot" / "has no capability") without consulting the actual available-capability set. "No pack reacted" is a routing fact; a task whose capability EXISTS but was misrouted records "misrouted, capability available", never an absence. A genuine gap is still recorded honestly.
- No phantom work (ADR-032): a branch proposing to build/extend a capability that already exists is not proposed — an observation that the capability is present (and the prior gap spurious) is recorded instead.
- Self-dispatched repair (ADR-036/037): the planner may turn the lab's OWN observed defects (a `routing_miss`; a finding the build tagged `code_defect`) into a GATED code-fix branch routed `codebase.code_task`, carrying the defect's evidence — lab-repairs-lab, not operator-hands-lab-each-bug. On activation the code worker AUTHORS the fix (ADR-037, amended by ADR-038): a propose_pr task with a brief but no candidate diff drives an LLM authoring step (on `setting.model.code_worker`) that reads the cloned files and emits, per changed file, the FULL new file content plus a regression test — NOT a unified diff. The lab then constructs the patch DETERMINISTICALLY (difflib over the cloned original) and PROVES it in the sandbox, retrying up to `setting.code_author_max_attempts` before recording an honest gap. The model supplies intent; the tooling supplies correct patch mechanics, so a hand-computed hunk header can never break `git apply` (the branch#1667 failure). Bounded: only the lab's OWN allowlisted repo (`yoheinakajima/activegraph-lab` — never the packs repo or core, ADR-005); only defects with concrete evidence; gated like any branch (the operator activates); only while the code lane is live; and capped at `setting.max_self_repair_branches` concurrent self-repair branches. The resulting `submit_pr` stays doubly-gated and MCP-excluded (a proven fix opens only a PENDING decision; a fix that fails its sandbox proof — supplied OR authored — opens none) — every PR is the operator's tap.

## Forks

- Forks anchor to committed events only.
- In-flight work stays with the parent branch.

## Dependencies

- `activegraph == 1.0.5.post2`
- `activegraph-packs @ git+https://github.com/yoheinakajima/activegraph-packs`, pinned to a commit SHA. Bumping the pin is a gated decision (ADR-005).
- `click >= 8.1`, `anthropic >= 0.34`, `openai >= 1.40`
- No numpy.

## Upstream friction

- Friction consuming activegraph-packs is evidence: record it as observations under the mission; propose upstream issues as artifacts. Never edit the packs repo directly (ADR-005).

## Claims

- No benchmark, performance, or capability claim in any artifact without linked evidence objects.
