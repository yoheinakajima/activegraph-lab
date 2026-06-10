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
