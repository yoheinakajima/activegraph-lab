# Changelog — lab pack

## Unreleased

- ADR-023 (the evt_1847/evt_1934 incident): the chat path's failure domain
  is now explicit — the message append is the only step that may fail a
  request, post-commit failures degrade to reply_pending + a
  chat_path_degraded observation, and storage.repair_sequences realigns a
  restored Postgres lineage's events sequence at boot (a row-level
  pg_restore leaves BIGSERIAL behind the rows; once nextval reached the
  restored block every append died with UniqueViolation AFTER the event
  entered the in-memory log). tools.py gains ensure_branch_thread_fn /
  append_branch_message_fn primitives; send_branch_message_fn composes
  them unchanged. Diagnostics: /lab/errors + MCP get_errors ring buffer,
  structured error responses (class + sanitized message). ADR-024: the
  server binds before the boot drain; /healthz reports the phase.

- MCP send_chat's reply wait is now a setting, `mcp_reply_wait_seconds`
  (default 15, seam-whitelisted): the fixed 60s wait exceeded claude.ai's
  client-side tool timeout, so the client errored before the structured
  `reply_pending` partial could be returned. The bound now comes in under
  the client's; a slow-reply test locks reply_pending-within-the-bound.
- Paused-boot fix (the evt_1702/1845/1846/1847 incident): the boot run
  cycle ALWAYS happens — pause state is rebuilt from the log BEFORE the
  first drain, the replay-requeued backlog is processed at boot, and
  resume drains immediately so lab.resumed takes effect in the running
  process. Paused gates which behaviors fire (everything but answer
  idles), never whether the worker runs. Locked by the new paused_boot
  fixture; the incident is queued as a live finding (diagnosed entirely
  from public log forensics over MCP).

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
