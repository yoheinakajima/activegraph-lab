# Changelog — lab pack

## Unreleased

- MCP send_chat hardening (ADR-016): the chat path is split into a commit
  phase (message lands and saves) and a bounded reply phase; when the reply
  misses the wait or the reply phase fails, the tool returns a structured
  partial success (`status=reply_pending`, message event ids, poll
  get_branch) instead of a generic error.
- Answer-subscription invariant made explicit and locked by fixtures: the
  answer behavior fires on operator authority (server-stamped sender),
  never on the literal `metadata.source` tag — operator, operator_via_mcp,
  and any future operator_via_* surface all draw a reply, exactly once.
- Live-finding backfill: findings discovered after first deploy
  (`LIVE_FINDINGS`, keyed) are appended idempotently at boot on resumed
  runtimes; first entry records the MCP predicate-gap diagnosis.

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
