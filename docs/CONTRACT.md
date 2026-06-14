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

- In any chat path, the message append is the ONLY step whose failure may fail the request (ADR-023). Post-commit failures degrade to `reply_pending` with the committed message ids — never an error after a successful append. The reply runs on the worker regardless of the client's fate.

## Authority

- One bit per capability: `auto` or `gated`.
- Publishing and self-modification are always gated. Self-modification additionally requires its enabling flag (`LAB_ALLOW_GRAPH_CODE=1` for graph code) — an approved decision alone is not enough (ADR-012).
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

- The lab never calls domain packs directly. Work is dispatched as core task objects with routing tags; packs react or a capability-gap observation is recorded (ADR-006). Tasks route by their ACTION verb (ADR-025/ADR-006): reading source to verify/check/examine a claim is `research.deep_research`; only WRITE/MODIFY/GENERATE-code intent is `codebase.code_task`. A mention of code, files, or a repo is not a request to write code.
- Workers emit a progress event at least every 60 seconds, or declare the current step uninterruptible.
- All external fetches go through tool_gateway.
- Honesty about capability (ADR-031): no behavior asserts a capability is absent ("lacks the means" / "cannot" / "has no capability") without consulting the actual available-capability set. "No pack reacted" is a routing fact; a task whose capability EXISTS but was misrouted records "misrouted, capability available", never an absence. A genuine gap is still recorded honestly.
- No phantom work (ADR-032): a branch proposing to build/extend a capability that already exists is not proposed — an observation that the capability is present (and the prior gap spurious) is recorded instead.

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
