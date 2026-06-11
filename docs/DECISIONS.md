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
- Note (2026-06-11): selection now reads `LAB_DATABASE_URL` first, falling back to `DATABASE_URL`, then SQLite. Replit reserves `DATABASE_URL` when its managed-Postgres module is present, colliding with explicit secrets at publish time; the lab sidesteps the reserved name rather than fighting the platform, and the fallback preserves every existing environment. `LAB_DATABASE_URL` is a credential exactly like `DATABASE_URL` (ADR-011); the sentinel audit covers both.
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
- Note (2026-06-11, ADR-023 amendment to error hygiene): an exception's CLASS and a sanitized one-line MESSAGE are not secrets — withholding them turned a one-line diagnosis into three sessions of log forensics. Error responses and the `/lab/errors` projection carry both. Payload contents, env values, URLs/DSNs (credentials ride in them), and filesystem paths remain secrets: `_sanitize_error_text` redacts them mechanically and the sentinel audit greps the projection like every other public surface. The OAuth endpoints keep their fixed bodies — an unauthenticated password-guessing surface gets no oracle.

## ADR-012: Code residency — the four-tier ladder

- Status: accepted
- Date: 2026-06-11
- Decision: Code lives in one of four tiers. KERNEL (GitHub only, forever): the gate behavior, auth middleware, event loop/runtime wiring, replay machinery, the storage adapter, and the seam/code loaders themselves — the thing that governs self-modification is never subject to it. SEAMS (graph-stored, gated): prompts, feed narration templates, whitelisted behavior settings values. GRAPH CODE (graph-stored, gated, dark by default): behaviors and tools drafted as artifacts, sandbox-tested, promoted only through an approved decision AND `LAB_ALLOW_GRAPH_CODE=1`. PLUMBING (GitHub, pragmatically): server, UI, scripts.
- Enforcement: `lab_pack/kernel.py` is the manifest of protected module paths; the seam and code loaders refuse any graph artifact that names, imports from, shadows, or monkeypatches a manifest entry. The manifest itself is kernel. The seam-eligible settings whitelist is kernel.
- Rationale: Graph-stored seams and code give perfect replay provenance — the code that ran is in the log that replays. The kernel stays in git for bootstrap and security. Self-modification is a capability like any other: one bit, gated, absolute (ADR-002).

## ADR-013: Public site structure — the blog is the front door

- Status: accepted
- Date: 2026-06-10
- Decision: The root of the lab's domain is a blog: published posts rendered server-side from the graph (artifacts with status=published), newest first. Each post page renders the post plus its provenance subgraph: originating branch, evidence observations, chat messages on that branch, the publish decision, and prior draft versions. The notebook feed moves to /lab and shows everything — all branches (active, proposed, decided, archived), all chats, all decisions including resolved ones.
- Rationale: The work is the product; the blog is the front door, the notebook is the open workshop behind it. Rendering posts from the graph (not the drafts/ mirror) keeps the UI a pure projection of the log, and "Show the work" makes the inspectability pitch concrete on every post.
- Note: publishing now appends an `artifact.published` marker event through the gate path. Marker events (here and ADR-015) carry no graph-state projection — they are log entries the lab's own projections read — so the "lab adds no event types" line in ARCHITECTURE.md is amended to "no state-bearing event types".

## ADR-014: Editorial policy — notes accumulate, research is earned

- Status: accepted
- Date: 2026-06-10
- Decision: Artifacts of kind=blog_draft gain a post_kind field: note | research | build. Small single-finding posts are notes; multi-evidence or multi-branch syntheses are research; posts about constructing the lab itself are build. Notes do not auto-draft one-per-finding: findings accumulate as queued observations, and a digest behavior proposes one combined note when unpublished queued findings reach setting.digest_min_findings (default 3). Research/build drafts require a decided branch with at least setting.research_min_evidence (default 3) linked evidence objects, or a synthesis of ≥2 decided branches whose combined evidence meets the same bar. When pending publish decisions reach setting.max_drafts_pending (default 5), automatic drafting idles and records an observation — the operator's attention is also a budget. The operator can always request a draft on a branch via chat; an explicit request is spent attention, so it bypasses the pending cap.
- Rationale: Fire-per-finding floods the inbox and produces thin posts. Accumulation plus thresholds makes volume a policy, and every threshold is a seam setting — tuning editorial policy is self-modification through the gate, not a redeploy.

## ADR-015: Operator controls — pause and a daily cost ceiling

- Status: accepted
- Date: 2026-06-10
- Decision: A global pause and a daily cost ceiling, both persisted as events and rebuilt from the log at boot (restart-proof, the same mechanism as the daily call cap). POST /lab/pause and /lab/resume append lab.paused / lab.resumed marker events; mutations are operator-token-gated like everything else. While paused, every LLM behavior except answer idles, recording one behavior-skipped observation per behavior per pause episode (not per event); answer stays live — the operator can always talk to the lab, and answer is cheap and budget-counted. The cost ceiling uses activegraph's native cost accounting (cost_usd on llm.responded events), capped by setting.daily_cost_cap_usd (default 5.00, seam-whitelisted); blocked-by-cost attempts log like blocked-by-count.
- Rationale: The kill switch and the spend limit must survive restarts, which means they live in the log, not in process memory or env vars. Keeping answer alive while paused preserves the one channel the operator steers through.
- Note (2026-06-11): a production process that inherited paused=true from the log booted with a dead worker — the resumed-boot path only drained the runtime when findings were backfilled, so the backlog that replay requeues (every event after the log's last runtime.idle) sat parked, and the operator's lab.resumed (an event nothing subscribes to) woke no run cycle (evt_1702/1845/1846/1847 vs evt_1590/1616, diagnosed over MCP). Amendment, locked by the paused_boot fixture: pause state rebuilds from the log BEFORE the first drain; the boot run cycle always happens; resume drains immediately. Paused gates which behaviors fire (everything but answer idles), never whether the worker runs.

## ADR-016: MCP surface — external assistants read the lab, the operator's authority stays human

- Status: accepted
- Date: 2026-06-10
- Decision: The lab exposes an MCP server (streamable HTTP) at `/mcp` on the existing lab server, so external AI assistants can read the lab and, with operator authority, talk to it. Tools come in two tiers. READ (no special authority — this data is already public via the blog, /lab, and /trace): `get_status` (the healthz projection), `get_feed` (paginated), `get_branch` (full timeline for a branch id, chats interleaved), `get_pending_decisions` (with evidence summaries), `get_post` (published post + provenance), `list_posts`, `list_seams`. OPERATOR (requires authority): `send_chat` — message a branch as the operator; the message lands in the public log like any operator message (ADR-011). EXCLUDED BY DESIGN: approve/reject decisions, pause/resume, and seam promotion are deliberately NOT tools. The inbox is the one place only the human operator exists; routing approval through an external AI would reduce the gate's guarantee to "an AI approved it." This is a recorded non-capability, not an omission.
- Access: the `/mcp` endpoint requires `Authorization: Bearer LAB_MCP_TOKEN` — a NEW secret, distinct from `LAB_OPERATOR_TOKEN`, revocable independently. It never grants inbox or pause authority even though it permits `send_chat`: `LAB_MCP_TOKEN` is refused on `/lab/decision` and `/lab/pause`, and `LAB_OPERATOR_TOKEN` is refused on `/mcp` (strict two-way separation; covered by a cross-authority test). Constant-time compare and 401/403 semantics match the rest of the server; `/mcp` mutations share the existing in-memory rate limiter. MCP-originated chats are tagged `source=operator_via_mcp` in the event payload so the public log distinguishes the human from their assistant.
- Implementation: the streamable-HTTP protocol (JSON-RPC 2.0 over POST, single-JSON responses — a mode the spec permits) is implemented minimally by hand in `server/mcp.py`. The official Python MCP SDK was evaluated and declined: its streamable-HTTP transport is ASGI-only, and the lab server is stdlib `http.server` (kernel wiring, ADR-012) — adopting the SDK would mean adding a Starlette/uvicorn stack and restructuring kernel server wiring for one endpoint. No new dependency is taken; if the server ever moves to ASGI, swapping in the SDK is the natural follow-up. No new state: every tool is a projection of the event log or a call into the existing chat path. The auth check for `/mcp` lives in `server/lab_server.py` (already in the kernel manifest); the manifest itself is not modified — graph code still cannot reach the token because `os.environ` is manifest-protected.
- Rationale: the lab's pitch is an inspectable agent; an MCP surface makes the inspection programmable for the operator's own assistants without widening the gate. Splitting the token keeps "my assistant can read and chat" revocable without touching the operator's own authority.
- Amendment (2026-06-10): `/mcp/<token>` (path segment) is accepted as an alternate presentation of the same credential — constant-time-compared against `LAB_MCP_TOKEN`, identical authority to the header path (`send_chat` yes; decisions/pause still refused), wrong path token → 401. Reason: claude.ai's custom-connector UI cannot send a static bearer header (its credential fields are OAuth-only), which made header-only `/mcp` unusable from the Claude clients; header auth stays for everything else. Security note: the URL is now a credential — treat connector URLs like tokens (they can land in client configs, browser history, and intermediary logs). On the lab's side the token is never echoed or stored: access logging is disabled, error bodies carry only fixed messages, and the sentinel audit exercises the URL-token path. Rotation = rotate `LAB_MCP_TOKEN`; both presentations rotate together.
- Note (2026-06-11): send_chat's bounded reply wait is `setting.mcp_reply_wait_seconds` (default 15, seam-whitelisted in lab_pack/kernel.py). The original fixed 60s wait exceeded claude.ai's own tool timeout, so the client errored before the structured reply_pending partial could be returned — the bound must come in under the client's, and tuning it is self-modification through the gate.
- OPEN: OAuth 2.1 with Dynamic Client Registration is the eventual proper implementation for the Claude clients; the URL token is the pragmatic bridge until then. (Resolved by ADR-017.)

## ADR-017: OAuth 2.1 + DCR on the MCP surface — stateless, single-operator, keyed from LAB_MCP_TOKEN

- Status: accepted
- Date: 2026-06-11
- Amends: ADR-016 (resolves its OPEN item; everything else in ADR-016 — the tool tiers, the excluded-by-design gate authority, the two-way token separation — stands unchanged).
- Decision: the MCP surface gains a single-operator OAuth 2.1 authorization server so claude.ai's web connector (whose credential fields are OAuth-only) can complete its handshake against a plain `/mcp` URL. Design principle: STATELESS — no token table, no client table, nothing secret ever written to the event log or any storage. Every credential the server issues (client_id, authorization code, access/refresh token) is an HMAC-SHA256-signed payload (type, client_id, scope, expiry), keyed from `LAB_MCP_TOKEN` via a fixed derivation, and verified later by recomputing the signature. Rotating `LAB_MCP_TOKEN` therefore revokes every client registration, code, and token at once — the same revocation story as the legacy presentations, which REMAIN: bearer-header `/mcp` and path-token `/mcp/<token>` keep working with identical authority (`send_chat` yes; decisions/pause/seams still excluded).
- Endpoints (all on the existing stdlib `http.server`, no ASGI, no new dependency — the ADR-016 reasoning about the MCP SDK still applies): `GET /.well-known/oauth-authorization-server` and `GET /.well-known/oauth-protected-resource` (RFC 8414 / RFC 9728 metadata, also served with the `/mcp` path suffix; PKCE S256 required, `token_endpoint_auth_methods_supported: ["none"]` — public clients, no client_secret); `POST /register` (RFC 7591 DCR: returns a deterministic client_id, the HMAC of the sorted redirect_uris — idempotent re-registration with no registry; redirect_uris must be https claude.ai/claude.com callbacks or localhost, anything else is refused); `GET /authorize` (a minimal HTML page with one password field — the OPERATOR pastes `LAB_MCP_TOKEN`; correct token, constant-time-compared → 302 to the redirect_uri with a 60-second HMAC-signed code binding client_id + redirect_uri + PKCE challenge; wrong token → 401 with a fixed body, no oracle detail); `POST /token` (code + PKCE verifier → 24h access token + 30d refresh token; refresh_token grant rotates the pair; fixed RFC 6749 error bodies, constant-time comparisons throughout).
- `/mcp` 401s carry `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"` per the MCP auth spec, so clients discover the flow. Bearer routing on `/mcp`: the legacy `LAB_MCP_TOKEN` compare runs first; a credential shaped like a signed blob then takes OAuth verification (failure → 401 `invalid_token` so clients refresh); any other mismatch keeps the legacy 403.
- Hard rule (sentinel-enforced): no token, code, or `LAB_MCP_TOKEN`-derived value appears in any event payload, log line, or error body. The 302 Location and the `/token` response body are the intended delivery channels and the only places a credential appears; `scripts/test_public_safety.py` runs the full flow end to end and audits the minted code, both tokens, their signature halves, and the derived signing key against the whole public corpus.
- Implementation: the protocol lives in `server/oauth.py`, which holds NO secret — every function takes the signing key as an explicit argument, derived from `LAB_MCP_TOKEN` only inside `server/lab_server.py` (kernel, ADR-012). Importing `server.oauth` therefore yields inert functions: graph code cannot reach the key (`os.environ` and `server.lab_server` are manifest-protected), so the kernel manifest itself is again not modified — the same posture ADR-016 took with `server/mcp.py`. The `/register`, `/token`, and `/authorize` POSTs share the existing in-memory mutation rate limiter (they are unauthenticated endpoints; the authorize form is the password-guessing surface).
- Accepted trade: statelessness means an authorization code cannot be marked used — replay inside its 60-second window with the same PKCE verifier mints a token. The code only ever transits the operator-initiated redirect to an allowlisted claude.ai/claude.com/localhost callback, the verifier never leaves the client, and the alternative is a token table — the thing this design exists to avoid. Likewise individual tokens cannot be revoked: revocation is rotation, which is already the lab's story.
- Rationale: the URL-token bridge made the connector URL itself a credential; OAuth removes the secret from the URL entirely (the operator authenticates once on the authorize page) without adding storage, a framework, or any new authority. One secret still rules the surface, and the human gate (ADR-016's excluded-by-design list) is untouched.

## ADR-018: The charter is a seam — operator-authored, versioned, injected verbatim

- Status: accepted
- Date: 2026-06-11
- Decision: the lab gains an operator-authored mission charter as seam `charter.mission` — versioned, gated, hot-loaded, replay-recorded, and injected VERBATIM (a delimited CHARTER block) into the context assembly of exactly three behaviors: plan, interpret, and draft_writer. v1 ships as the FILE DEFAULT (`lab_pack/prompts/charter.md` — operator-authored content, committed by the operator's own build pipeline), so unlike every other seam the file default counts as version 1, not 0: the first graph-stored proposal is v2, parented on the file's v1, and it activates only through the self_modify gate like any seam. `charter.mission` is the only whitelisted charter surface; the kernel-manifest body scan applies to charter bodies exactly as to prompts.
- Replay fidelity: the three behaviors stamp `charter.mission` alongside their prompt version in `seam_versions` on every output, so the log records which charter was in force for each decision the model made — and since replay never re-fires behaviors, those records replay verbatim (the same mechanism as prompt seams, ADR-012 Phase 4).
- Exclusions: answer does NOT receive the charter — it reports graph state to the operator and must not restate portfolio priorities as if they were findings. The charter composes WITH prompt seams (description = active prompt body + active charter block), so promoting either surface independently recomposes the live context without restart.
- Mechanical note: the upstream prompt loader (`load_prompts_from_dir`) refuses any `prompts/*.md` without TOML frontmatter, so charter.md carries the standard two-line frontmatter; the BODY — the thing injected, resolved, and versioned — is exactly the operator's charter text. The charter prompt name is excluded from provider-side behavior identification (its body appears inside three behaviors' contexts).
- Rationale: the lab's portfolio judgment (what to verify, build, measure; how much introspection is too much; what voice is honest) is policy, not plumbing — it belongs in an inspectable, versioned, gated surface the lab can eventually propose changes to itself, not scattered through prompt prose the operator would have to diff by hand.

## ADR-019: Per-behavior model routing as seam settings; budget defaults raised behind a kernel ceiling

- Status: accepted
- Date: 2026-06-11
- Decision: each lab llm_behavior's model is a seam setting — `setting.model.plan`, `setting.model.interpret`, `setting.model.draft_writer`, `setting.model.answer`, plus `setting.model.default` for behaviors without their own entry (all seam-whitelisted; dotted seam names map to underscored LabSettings fields). Defaults: plan/interpret/draft_writer = `claude-opus-4-8` (the deliberate, expensive plane); answer/default = `claude-sonnet-4-20250514` (the fast plane). The resolution is stamped onto `behavior.model` at boot (after `apply_approved`) and on every `setting.model.*` hot-load, so a seam override reroutes without restart — and because the runtime reads `behavior.model` at prompt assembly and records it on every `llm.requested` event, the per-behavior resolution is in the log natively, with no lab-side event plumbing. A routed model the active provider does not recognize is skipped (the provider default stays) rather than raised: cross-provider misconfiguration must not kill the worker.
- Cost accounting: stays the runtime's native per-model reporting — `cost_usd` on `llm.responded`, priced per model family by the provider — never a hardcoded single price. The daily budget rebuild (ADR-015) is unchanged and therefore correct across mixed-model days by construction.
- Budget defaults: `daily_cost_cap_usd` default rises 5.00 → 50.00 (opus-priced deliberate work needs headroom). NEW kernel constant `ABSOLUTE_DAILY_COST_CEILING_USD = 100.00` (`lab_pack/kernel.py`): enforced at the budget path's single enforcement point (`effective_cost_cap`), NOT seam-modifiable, NOT MCP-modifiable — every cap mechanism (settings default, approved `setting.daily_cost_cap_usd` seam, the MCP `set_budget` control of ADR-021) clamps to it. Raising the ceiling is a git change, never a graph one.
- Found while wiring this: the pack loader registers FRESH canonical-named copies of every behavior (`lab.plan`, …) — deliberately, upstream — so the existing prompt-seam hot-load, which mutated only the module originals, never reached the live runtime's registration (the seams fixture asserted the original's attribute, not the registered copy's). Fixed here for prompts, the charter, and model routing alike: `bind_live_behaviors` captures the runtime's copies at every build/resume, and all seam mutation paths write through `behaviors_named` to both objects; the fixtures now assert against `rt.get_behavior("lab.<name>")`.
- Rationale: which model thinks for which behavior is policy the lab should be able to argue about and change through the gate — but spend needs a floor the graph cannot move, or a single approved seam (or compromised assistant) could turn the routing table into a blank check.

ADR-020–022: reserved for the in-flight consolidation session (research worker, MCP expansion, GitHub read).

## ADR-023: Chat path failure domain — the append is the only fail point; everything else degrades, diagnosably

- Status: accepted
- Date: 2026-06-11
- Decision: in any chat path (POST /chat and MCP send_chat), the comm_message append is the ONLY step whose failure may fail the request — and even that failure is structured (exception class + sanitized one-line message, per the ADR-011 amendment noted there), never a generic 500. Everything post-commit is individually guarded: thread/relation upkeep, registry-cache updates, save, response assembly, and the reply drain. A post-commit failure records a sanitized entry on the diagnostics ring buffer plus a best-effort `chat_path_degraded` observation, and the response is `status=reply_pending` carrying the committed message ids — NEVER an error after a successful append. The reply is always triggered on the runtime worker once the append succeeded: a bounded-wait timeout leaves the job queued, a degraded path submits it fire-and-forget, and a client disconnect is irrelevant to its completion. Thread resolution no longer trusts the in-process discusses cache alone: on a miss it reads the log's relations (ADR-008 decode) before creating a new thread — the cache is a cache, the log is the truth.
- Leaf cause (the incident this hardens against): a pg_dump/pg_restore'd lineage left the `events.seq` BIGSERIAL sequence behind the restored rows. Boot appends landed in the leading seq gap and succeeded; once nextval reached the restored block, EVERY durable append raised `psycopg.errors.UniqueViolation (events_pkey)` from `store.append` inside `graph.emit` — AFTER the event entered the in-memory log. Projections (including MCP forensics) showed the message as committed while durability had silently stopped, the request 500'd generically, and the reply drain never started. SQLite cannot reproduce it: AUTOINCREMENT derives the next rowid from the table; BIGSERIAL trusts nextval. Fix: `lab_pack/storage.repair_sequences` realigns the sequence past max(seq) at resumed boot, before the runtime opens the store. The storage adapter is the one module allowed to touch a framework table (ADR-009 already makes it the only backend-aware code); projections still never read framework tables.
- Diagnostics: a volatile in-process ring buffer (last 50 entries: ts, class, sanitized message, request kind, related event ids) served read-only at `GET /lab/errors` and the MCP READ tool `get_errors` (added to the ADR-016 READ tier; it grants nothing the stderr log didn't already hold, minus the secrets). Deliberately NOT log-derived and reachable before readiness: its job is to stay readable when appending to the log — or booting the runtime — is the thing that failed. This is a scoped exception to the "all user-visible state derives from the event log" invariant, recorded in CONTRACT.md: memory only, lost on restart, never authoritative.
- Rationale: the incident took three sessions to corner because the failure was post-commit, the error was generic, and the only divergence signal (in-memory log vs durable rows) had no projection. Splitting the failure domain makes the committed/not-committed boundary explicit in every response; the ring buffer plus structured errors turn the next one into a three-minute read.

## ADR-024: Bind first — readiness is a phase, not a precondition

- Status: accepted
- Date: 2026-06-11
- Decision: the server binds its socket BEFORE building the runtime; the boot (replay, registry rebuild, drain) runs on the runtime worker behind it. `/healthz` answers from bind time with `ready=false` and the boot phase (`starting → loading → draining → ready | failed`); `/lab/errors` is likewise never gated (ADR-023). Every other request gets `503 + Retry-After: 5` with the phase in the body until the drain completes; a failed boot reports `phase=failed` with the sanitized boot error on `/healthz` and keeps refusing with 503.
- Rationale: the resumed-boot drain grows with the log; binding after it will eventually exceed any deploy health-grace window (Replit flagged exactly this). Health checks need the socket, not the graph — and an honest "starting, retry in 5" beats a dead port. Locked by the readiness fixture: requests during a synthetic slow drain get 503-with-retry, healthz answers throughout, ready flips when the drain ends.
