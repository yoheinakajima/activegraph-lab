# Changelog — lab pack

## Unreleased

- Decision resolution carries rationale (ADR-026): `POST /lab/decision`
  takes an optional rationale; the ONE resolution patch event records
  `metadata.resolution_rationale` + `resolved_by=operator` (the
  proposer's rationale field stays untouched; no placeholder when
  absent). The rejected-decision registry stores and exposes it —
  surviving the resume rebuild — so seam proposals and draft-request
  item contexts cite the OPERATOR's reasons, not just the proposer's
  pitch; the feed narrates resolutions from the operator's reason.
  Chat approve/reject resolves through the same path (the message is
  the rationale). The UI's approve/reject buttons open an optional,
  skippable rationale field. New MCP operator-tier tool
  `annotate_decision(decision_id, note)`: a public,
  operator_via_mcp-attributed annotation on a PENDING decision — the
  handler can only create the observation and append its ref, no code
  path touches status. On resolution, pending annotations link into
  the decision's evidence and the UI prefills the rationale from the
  most recent one. approve/reject remain EXCLUDED from MCP; annotation
  is commentary, not authority. Backfill: nothing — append-only.
  Locked by the decision_rationale fixture; covered in test_mcp,
  the sentinel audit, and check_ui (two-step resolve form + prefill).

- Truthful steering replies (ADR-025; the evt_3676 incident): steering
  mutations apply before the reply is composed, the reply reports
  POST-mutation state and cites the new `lab.steering_applied` marker
  event for every applied verb, a no-op verb says so, and an action
  request no verb supports draws an explicit refusal naming the
  supported verb set — the model's pre-mutation narration is used only
  for questions about state (its prompt now forbids action claims).
  Verb matching is word-boundary ("activate" no longer hides inside
  "deactivate", "pause" inside "unpause"). Locked by the
  truthful_steering fixture.
- Steering verbs `activate` (proposed/scoped → active, recording the
  operator rationale as a branch_activated observation; the existing
  dispatch reacts) and `deactivate` (active → proposed) — operator
  authority, MCP-allowed (reversible, like pause; ADR-021's argument).
  End-to-end locked in the research_worker fixture: MCP activate →
  dispatch → worker claims, fetches, synthesizes, completes.
- Decision keying closed correctly (ADR-025): pending decisions index by
  branch as well as subject_ref (chat `approve` on a publish decision —
  subject = the artifact — was a silent no-op). Exactly one pending
  applies; multiple list ids without mutating; zero is an honest no-op;
  and operator_via_mcp messages are REFUSED for approve/reject — the
  inbox stays human-only (ADR-016/021).
- The 1/30 crawl stall diagnosed and fixed (mission#1 evt_768 /
  source#45): the gateway stores the fetch envelope JSON-encoded and
  truncated at max_output_chars (default 10K), a real page's envelope
  was cut mid-string, ingest's json.loads fallback treated the ESCAPED
  envelope as HTML, and `href=\"...\"` matched no link — queued=0
  forever. Fixed in layers: `_parse_fetch_envelope` salvages truncated
  envelopes (diagnosis comment above it), link extraction is
  anchor-scoped (bare `href=` also matched `<link rel=preload>` asset
  tags — dozens of same-host /_next/ chunks that would burn the page
  budget) and fragment-tolerant (`/docs#install` resolves to `/docs`
  instead of being dropped), and bundle.load_lab_packs sizes the
  gateway's max_output_chars so a full envelope survives storage.
  Depth<=2 / page<=30 caps stand; existing junk-claim observations are
  untouched (append-only log). New steering verb `recrawl` creates a
  crawl_request scoped to the mission target_url for a fresh crawl
  episode — replay never re-fires behaviors, so a resumed lab needs the
  nudge. Locked by the crawl_stall fixture.
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
