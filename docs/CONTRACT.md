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

## Code residency (ADR-012)

- KERNEL stays in git forever: gate, auth, runtime wiring, replay, storage adapter, loaders, the manifest (`lab_pack/kernel.py`). The thing that governs self-modification is never subject to it.
- SEAMS and GRAPH CODE live in the graph, gated; PLUMBING in git. Loaders refuse artifacts referencing the kernel manifest.

## Messages and steering

- A user message is an event in the branch's log (a comm_message in a thread that discusses the branch — ADR-004).
- Replies come from a fast answer behavior that reads graph state, never blocks on running work, and stamps its event horizon.
- Steering takes effect at event boundaries.

## Workers

- The lab never calls domain packs directly. Work is dispatched as core task objects with routing tags; packs react or a capability-gap observation is recorded (ADR-006).
- Workers emit a progress event at least every 60 seconds, or declare the current step uninterruptible.
- All external fetches go through tool_gateway.

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
