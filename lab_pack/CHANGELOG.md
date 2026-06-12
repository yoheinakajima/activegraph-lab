# Changelog — lab pack

## Unreleased

- Seam-proposal truncation + evidence relevance (decision#195 /
  artifact#194): the chat-triggered seam_proposal_request capped the
  operator's message at 500 chars, so the seam_writer drafted a charter
  v2 from an excerpt — the proposed body cut off mid-VERBATIM and
  resumed with v1 text. The request now carries the operator message
  and the current body IN FULL (the seam_writer's view serializes the
  observation whole; an excerpt there IS a truncation in the proposal).
  Text the operator marks VERBATIM (`VERBATIM:` … `END VERBATIM`, or
  to end of message) rides the request as verbatim_sections, the
  seam_writer prompt instructs inclusion without paraphrase, and a
  post-generation check requires it intact in the body (substring after
  whitespace normalization) — failure opens NO proposal: a
  seam_proposal_failed observation records the diff (matched/expected
  chars + missing tail) and a lab.seam_writer reply lands in the branch
  thread (the chat path returns it). Second defect, same proposal:
  evidence selection is now seam-relevant — the operator message
  always; rejected decisions only when the target is
  prompt.draft_writer (publish rejections) or the decision references
  the same seam, so charter proposals no longer cite publish
  rejections. Locked by the seam_verbatim fixture (700-char section
  end-to-end intact; tampered generation blocked; charter evidence
  clean).

- Model-parameter compatibility (the Opus incident): ADR-019 routing
  seams can point a behavior at a model that rejects the lab's hardcoded
  temperature (400 "may only be set to 1"), and the failure was misfiled
  as llm_parse_failure. The lab now declares no temperature anywhere
  (six hardcoded kwargs removed); LabProviderWrapper forwards a
  framework-default temperature as the server default — the
  wire-equivalent of omitting the field through the pinned providers
  (ADR-005) — and on a 400 naming an unsupported/deprecated parameter
  strips it and retries exactly once, recording the strip on the
  llm.responded payload (provider_meta.lab_param_stripped). Failure
  domains split: new anomaly kind "call" → llm_call_failure for
  provider/API/network errors; llm_parse_failure reserved for output
  that arrived but didn't parse. Locked by the model_params fixture;
  the hazard is queued as a keyed live finding (upstream candidate:
  parameter handling belongs next to the provider's HTTP assembly).

- Store connection resilience (ADR-009 note; the Neon idle-suspend
  incident, twice): PostgresEventStore's single boot-lifetime connection
  dies when serverless Postgres suspends an idle compute — first write
  fails AdminShutdown, every later one OperationalError until restart.
  `storage.harden_store` (armed at server boot, idempotent) reconnects
  and retries exactly once on connection-class errors; constraint
  violations are never retried (which also makes a retried append
  double-commit-safe via UNIQUE(id, run_id)); a second failure surfaces
  structured (ADR-023). Each reconnect records `store_reconnected` on
  the diagnostics ring buffer, never the event log; long-idle appends
  get a SELECT 1 probe first. Locked by the reconnect fixtures in
  test_chat_robustness (policy everywhere; backend-kill end to end under
  LAB_TEST_PG_URL) and the store-level kill/duplicate/double-failure
  fixtures in test_postgres; the upstream candidate is queued in
  LIVE_FINDINGS.

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
