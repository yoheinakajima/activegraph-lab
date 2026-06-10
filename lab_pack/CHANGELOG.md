# Changelog — lab pack

## v0.1.0 (2026-06-10)

- Initial release: mission/branch/decision object types, six relations,
  six behaviors (ingest, plan, work, interpret, gate, answer).
- Crawl through tool_gateway capability calls (depth ≤ 2, pages ≤ 30,
  progress event per page).
- Emergent work dispatch via core tasks with routing tags; capability-gap
  observations when no pack reacts (ADR-006).
- Gated promote/publish decisions; publish without approval reverted.
- Branch-thread answers with event-horizon stamps; deterministic steering
  (pause/resume/approve/reject).
- Deterministic fixtures, no API key required.
