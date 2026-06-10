# DECISIONS

Architecture decision records. Format per entry:

```
## ADR-NNN: Title
- Status: accepted | superseded by ADR-MMM
- Date: YYYY-MM-DD
- Decision: one sentence.
- Rationale: short paragraph.
```

New ADRs append to the end. Changing a CONTRACT.md invariant requires an ADR here and, once the runtime exists, a decision object in the graph.

## ADR-001: Five object types

- Status: accepted
- Date: 2026-06-10
- Decision: The schema is exactly mission, branch, artifact, evidence, decision; adding a type is a gated decision.
- Rationale: Schema growth is itself an experiment. Starting minimal forces every proposed type to argue for itself with evidence from real use, and the gate makes that argument a recorded event rather than a drive-by commit.

## ADR-002: One-bit authority

- Status: accepted
- Date: 2026-06-10
- Decision: Each capability is either auto or gated; publishing and self-modification are always gated.
- Rationale: Maturity ladders (trust levels, graduated autonomy tiers) are bureaucracy the lab can evolve later if evidence demands it. One bit is auditable at a glance and cheap to flip per capability via a gated decision.

## ADR-003: Lab is a layered pack on core

- Status: accepted; supersedes the "exactly five object types" invariant of ADR-001
- Date: 2026-06-10
- Decision: The lab is a layered pack on the activegraph-packs core pack. It adds exactly THREE object types (mission, branch, decision) and reuses core primitives: core artifact for outputs, core observation + evaluation for evidence, core task for work dispatch, core source for ingested pages/repos/papers.
- Rationale: Core is the lingua franca; a parallel ontology would violate the conventions the lab is built on.
- New invariant: adding a fourth lab object type requires a gated decision object AND an ADR.

## ADR-004: No lab_interface pack

- Status: accepted
- Date: 2026-06-10
- Decision: The upstream communication + chat packs provide threads, messages, intents, and message injection. Thread = branch is one relation (discusses: thread → branch) plus one answer behavior in the lab pack. The lab_kernel/lab_interface split collapses into a single lab pack (`lab_pack/`).
- Rationale: The interface already exists upstream; we add a projection, not a layer.

## ADR-005: Standalone repo, packs as pinned git dependency

- Status: accepted
- Date: 2026-06-10
- Decision: activegraph-lab consumes activegraph-packs as a git dependency (`activegraph-packs @ git+https://github.com/yoheinakajima/activegraph-packs`, pinned to a commit SHA), with a single-line editable-install override for local dev. The lab is the first external consumer of the packs conventions; friction encountered while consuming them is evidence — record it as observations under the mission, and propose upstream issues as artifacts, never as direct edits to the packs repo. Bumping the pinned SHA is a gated decision the lab records about itself.
- Rationale: The packs repo is a reference library, not a product; the lab is a product.

## ADR-006: Workers via emergent coordination

- Status: accepted
- Date: 2026-06-10
- Decision: The lab never calls domain packs directly. Its work behavior writes core task objects with a routing convention (task.kind or tags); research and codebase pack behaviors react. No adapters/ directory.
- Rationale: Packs compose through graph state, not function calls — the packs repo's central design rule. An adapter layer would be a coordinator in disguise.

## ADR-007: 'paused' is a branch status

- Status: accepted
- Date: 2026-06-10
- Decision: The branch status enum gains `paused` (proposed|scoped|active|paused|interpreting|decided|archived), replacing the scoped + metadata.paused workaround for the pause steering verb.
- Rationale: Pause is an owner-visible state the feed must render and steering must round-trip (pause → resume). Overloading `scoped` made the projection lie about intent. Enum values on a lab-owned type are a code change plus this ADR — not a new object type, so no gated decision is required (ADR-003 gates types, not fields).

## ADR-008: Relation call-convention handling

- Status: accepted
- Date: 2026-06-10
- Decision: The lab writes relations in signature order — `add_relation(source_id, target_id, type)` — everywhere, and reads mixed graphs through one documented helper, `lab_pack/compat.py:decode_relation`, which discriminates per relation (object ids contain `#`, relation type names never do). All lab code that reads relations goes through this helper.
- Rationale: The packs repo is split on argument order (core/research/tool_gateway type-first; chat signature-order); a composed graph holds both encodings. Signature order is what runtime view traversal and relation queries require, so the lab writes it; the decode shim is quarantined in one place, linked to the friction observation seeded under the mission, and goes away if upstream standardizes (the lab's draft issue artifact proposes exactly that).

## ADR-009: Storage — native PostgresEventStore, selected at boot

- Status: accepted
- Date: 2026-06-11
- Decision: The event store is activegraph's native persistence layer (ag-coder pattern: a dedicated `activegraph` Postgres schema, framework-owned tables, fork/replay native, via `pip install "activegraph[postgres]"`). `DATABASE_URL` present → Postgres; absent → SQLite under `data/` (the dev/fixture default; fixtures stay keyless and deterministic). Backend selection lives in exactly one place (`lab_pack/storage.py`, kernel); no other code may know which store is active, and all projections read through runtime/event APIs, never raw SQL against framework tables.
- Rationale: The log is the source of truth; the store is the framework's concern, not the lab's. One selection point keeps the swap auditable and the rest of the codebase backend-blind.
- OPEN: memory_gateway's own store stays local-SQLite for now.

## ADR-010: Deployment — Replit, continuously running

- Status: accepted
- Date: 2026-06-11
- Decision: The lab deploys to Replit as a continuously running server; all durable state lives in managed Postgres (ADR-009), never the filesystem. A single operator authenticates with a bearer token (`LAB_OPERATOR_TOKEN`); all projections are publicly readable.
- Rationale: One operator, one always-on process, zero filesystem state makes redeploys idempotent: the schema either has events (resume) or it doesn't (seed).

## ADR-011: Public log policy

- Status: accepted
- Date: 2026-06-11
- Decision: Operator chat is part of the public log, deliberately. No secrets in any event payload, observation, artifact, boot log, or error path — enforced by an automated sentinel audit (DATABASE_URL is a credential too).
- Rationale: The lab's pitch is an inspectable agent; a private side-channel would undercut it. The cost is discipline about payloads, which the audit makes mechanical instead of aspirational.

## ADR-012: Code residency — the four-tier ladder

- Status: accepted
- Date: 2026-06-11
- Decision: Code lives in one of four tiers. KERNEL (GitHub only, forever): the gate behavior, auth middleware, event loop/runtime wiring, replay machinery, the storage adapter, and the seam/code loaders themselves — the thing that governs self-modification is never subject to it. SEAMS (graph-stored, gated): prompts, feed narration templates, whitelisted behavior settings values. GRAPH CODE (graph-stored, gated, dark by default): behaviors and tools drafted as artifacts, sandbox-tested, promoted only through an approved decision AND `LAB_ALLOW_GRAPH_CODE=1`. PLUMBING (GitHub, pragmatically): server, UI, scripts.
- Enforcement: `lab_pack/kernel.py` is the manifest of protected module paths; the seam and code loaders refuse any graph artifact that names, imports from, shadows, or monkeypatches a manifest entry. The manifest itself is kernel. The seam-eligible settings whitelist is kernel.
- Rationale: Graph-stored seams and code give perfect replay provenance — the code that ran is in the log that replays. The kernel stays in git for bootstrap and security. Self-modification is a capability like any other: one bit, gated, absolute (ADR-002).
