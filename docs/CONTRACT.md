# CONTRACT

Invariants. Changing any item below requires an ADR in docs/DECISIONS.md and, once the runtime exists, a decision object.

## Object types

- Exactly five: `mission`, `branch`, `artifact`, `evidence`, `decision`.
- Adding a type is a gated decision, recorded as an ADR and a decision object.

## Event log

- All user-visible state derives from the event log. The UI is a projection.
- No side database.

## Authority

- One bit per capability: `auto` or `gated`.
- Publishing and self-modification are always gated.

## Messages and steering

- A user message is an event in the branch's log.
- Replies come from a fast answer behavior that reads graph state and never blocks on running work.
- Steering takes effect at event boundaries.

## Workers

- Workers emit a progress event at least every 60 seconds, or declare the current step uninterruptible.

## Forks

- Forks anchor to committed events only.
- In-flight work stays with the parent branch.

## Dependencies

- `activegraph == 1.0.5.post2`
- `click >= 8.1`
- `anthropic >= 0.34`
- `openai >= 1.40`
- No numpy.

## Claims

- No benchmark, performance, or capability claim in any artifact without linked evidence objects.
