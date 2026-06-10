# ARCHITECTURE

Outline. Each section is a stub to be expanded in its milestone. CONTRACT.md governs.

## Packs

### lab_kernel

The agent's core loop as roughly five behaviors: `ingest` (pull external material into evidence), `plan` (read graph state, propose branches and next steps), `work` (dispatch to worker adapters, track progress events), `interpret` (turn worker output into evidence and draft artifacts), `gate` (surface gated decisions, apply outcomes). Exact behavior boundaries are settled during Milestone 1. OPEN: whether `gate` is a behavior or a property of the event dispatch layer.

### lab_interface

Two pieces: message ingestion (a user message becomes an event in the branch's log) and the answer behavior (fast path, reads graph state, never blocks on running work). Built in Milestone 2.

## Object types and relations

Five types per CONTRACT.md. One `mission` is the root. A `branch` belongs to the mission and may fork from another branch at a committed event. `evidence` belongs to a branch and links to sources. An `artifact` belongs to a branch and must link the evidence it relies on for any claim. A `decision` records a gate outcome and links the objects it governs. OPEN: whether relations are typed edges in the graph or fields on objects — follow whatever ActiveGraph 1.0.5.post2 idiomatically provides.

## Event taxonomy

Families expected: lifecycle (branch created/forked/closed), message (user/agent), work (dispatched/progress/completed/failed), object (created/updated/linked), gate (requested/approved/rejected). Names and payloads are fixed during Milestone 1 against the runtime's event model. The taxonomy is append-only once branch zero has run.

## Worker adapters

Workers live in `adapters/` behind a single contract: accept a task spec, emit progress events at least every 60 seconds (or declare the step uninterruptible), return results the `interpret` behavior turns into evidence. First two adapters: `deep-research` (Milestone 3) and `ag-coder` (deferred until self-modification is on the roadmap). OPEN: the task spec schema.

## Two-plane latency model

Two planes over one log. The fast plane: the answer behavior responds to messages in seconds by reading current graph state, never waiting on workers. The slow plane: planning and worker execution run at their own pace and commit events as they go. The fast plane sees the slow plane only through committed events, which is why answers carry event-horizon stamps (see INTERFACE.md).
