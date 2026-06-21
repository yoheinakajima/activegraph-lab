# ROADMAP

The public answer to "what is this project doing next." Shipped is shipped; the queue is in order; deferred means deferred (moving anything up needs an ADR).

## Shipped

- Lab pack: three lab types over core, eleven reactive behaviors, deterministic keyless fixtures (ADR-001..008).
- Storage + deploy: native Postgres event store selected at boot, Replit always-on, public log policy with sentinel audit (ADR-009..011).
- Code residency: kernel/seams/graph-code/plumbing ladder; seams live and gated; graph code dark behind `LAB_ALLOW_GRAPH_CODE` (ADR-012).
- Public surface: the blog at `/` with per-post provenance, the workshop at `/lab`, editorial discipline (digest/research thresholds), pause + cost controls rebuilt from the log (ADR-013..015).
- MCP: read tools + send_chat with OAuth 2.1 for the Claude clients (ADR-016/017); chat-path failure domains, `/lab/errors`, bind-first readiness, sequence repair (ADR-023/024).
- This consolidation: the operator charter as seam `charter.mission` (ADR-018); per-behavior model routing behind a $100/day kernel ceiling (ADR-019); the lab-local research worker — research branches now execute (ADR-020); chat-triggered seam proposals through the gate (Phase 4 rails); MCP `get_log`/`get_entity` + reversible operator controls, the inbox still human-only (ADR-021); read-only GitHub tools, allowlisted (ADR-022 rung 1).
- GitHub rung 2, the self-repair rails (ADR-035): a bounded repo sandbox (clean env — no inherited secrets — wall-clock + resource limits, allowlisted repos only; subprocess-hardened, E2B the documented future upgrade for untrusted code); the lab-local code worker reacting to `codebase.code_task` (clone → run a command → for a fix-task apply a diff and re-run to PROVE it → captured output as evidence); and the doubly-gated `submit_pr` decision (the lab drafts a fix + sandbox proof, opens a PENDING decision, and ONLY on operator approval is the separate `GITHUB_WRITE_TOKEN` exercised to open a PR the operator then merges on GitHub — no auto-merge; submit_pr is MCP-excluded like approve/reject). Both sentinel gates green; the loop is proven on the rails, not auto-fired.
- Self-dispatched code repair (ADR-036): the loop's FIRST mile, closed. The planner (`self_repair`) turns the lab's OWN observed defects — a `routing_miss` (ADR-031's code-bug verdict) or a finding tagged `code_defect` — into a GATED code-fix branch routed `codebase.code_task`, carrying the defect's evidence, no operator authoring. On activation the code worker proves the fix and opens a PENDING `submit_pr`; a fix that fails its proof opens none. Bounded to the lab's own allowlisted repo, capped at `setting.max_self_repair_branches`, dark unless the code lane is live; a `fix` steering verb is the operator's secondary front door. observation#1046 (the source-selection bug the lab logged itself) is the worked first case — after deploy the planner proposes its fix unaided.

- The daily heartbeat (ADR-044): the lab's FIRST standing behavior on a wall-clock cadence — a bounded, gated, killable departure from reactive-only. A once-per-window tick (`heartbeat_cadence`, default "daily", "off" disables it with no deploy) advances ONE step of an operator worklist (`heartbeat_worklist`, default the cheapest `advance_branch`) to bring in fresh input, then the existing reactive planner reacts. Spend-floored (`heartbeat_budget_ceiling_usd`, default $15), idempotent off the log across restarts, and GATE-PRESERVING: everything it causes lands as proposals in the human inbox — it never approves/promotes/submits_pr. Plumbing (droppable). Suite green (40/40).

## In progress

- Live operation: charter-guided branch proposals and research-worker runs against activegraph.ai, accumulating rejections, findings, and drafts in the public log.

## The queue, in order

1. **Operator rejections → the lab's first self_modify voice proposal** (the reserved episode). The rails exist (Phase 4); the episode is the lab proposing a better `prompt.draft_writer` in its own voice, evidenced by the two rejected drafts and the operator's provenance questions already in the log. The file prose stays untouched until that decision is approved — the approval must be a recorded decision, not a commit.
2. **Charter-guided external portfolio**: VERIFY / BUILD / MEASURE branches per CHARTER v1 — site claims tested empirically, demos in the AG Coder spirit, benchmark experiments in the Regimes spirit. Self-referential work stays a minority of open branches.
3. **The fork branch (flagship demo)**: verify "replay, fork, and diff any run" by forking one of the lab's own branches and diffing the outcomes — the claim, tested on the lab itself, with the diff as the artifact.
4. **Graph-code enablement** — `LAB_ALLOW_GRAPH_CODE=1` is set only when ALL of: an approved behavior draft exists that the operator actually wants live; the sandbox pipeline has rejected at least one draft for cause (the gate has demonstrated teeth); a rollback drill has been run (flag off → behavior gone, log intact); and the watchdog covers graph-code behaviors like any other.
5. **GitHub rung 2 (`submit_pr`)** — BUILT (ADR-035). The rails exist and are fixture-locked: the repo sandbox (secret-isolation sentinel-gated), the code worker on `codebase.code_task`, and the doubly-gated submit_pr decision. The first REAL exercise is operator-chosen: configure `GITHUB_WRITE_TOKEN` (fine-grained, lab repo only) and approve a submit_pr the lab opened. Until then the lab drafts + sandbox-proves + proposes, and the operator opens the PR by hand from the diff.

## The self-repair loop (ADR-035 built the rails; ADR-036 closed the first mile)

The path from a production bug to a merged fix, end to end. The rails were wired and proven by ADR-035; ADR-036 closed the FIRST mile — the planner now self-dispatches the proposal (step 2) from the lab's own observed defects, no operator authoring. Still NOT auto-merged: every PR is the operator's tap.

1. A production failure surfaces as a **finding** / **routing_miss** observation (the diagnostics ring buffer / a chat erratum / a verification branch's verdict / the capability self-check's ADR-031 routing verdict).
2. The lab **self-dispatches a fix** (ADR-036): the `self_repair` planner proposes a GATED `code_task` branch in the codebase lane carrying the repo, a proving command, and the defect's evidence — bounded to the lab's own repo, capped, gated. (The operator's `fix` verb is the secondary front door.)
3. The **code worker** clones the allowlisted repo into the **repo sandbox**, runs the command (baseline), applies the diff, and re-runs to **prove** it — captured output becomes attributed evidence, a `sandbox_proven` evaluation links to the branch.
4. The lab opens a **submit_pr** decision (the diff + the sandbox proof), status pending — the inbox.
5. The **operator approves** (the first gate) → the lab exercises `GITHUB_WRITE_TOKEN` to push the branch and open the **PR**.
6. The **operator reviews and merges** on GitHub (the second gate, a different system) — no auto-merge.
7. **Replit pulls** the merged main on its next deploy.

Worked example (the one this session was triggered by): branch#847 — "Fetch the actual implementation files … get_file on the event-log projection, replay, fork, and diff code paths … confirm the claims hold at the level of code." Its action is READ-TO-VERIFY, so origin/main's verb classifier correctly routes it `research.deep_research` (which carries get_file) — the production misroute to `codebase.code_task` was a deployment REGRESSION (a rebase reverted the router to the keyword version), not a classifier bug. The fix this session adds is twofold: a routing fixture locks branch#847's exact intent to research (so the regression fails CI loudly), AND the new code worker makes the branch answerable EITHER way — if a verification task still reaches the codebase lane, the worker records a `routing_miss` naming research and fails with an actionable verdict rather than dying in a dead lane. Verb-safe re-dispatch message after deploy: *"Re-verify the replay/fork/diff claims by reading the activegraph source — fetch the event-log projection and replay code paths and check the claims against them."* (fetch/read/check — no build/implement/fix verb, so it routes research.deep_research and the research worker's get_file fetches the files).

6. **Trigger-debouncing / fan-out reduction** (ADR-044 follow-up, GATES unattended heartbeat recrawl). The reactive pipeline has a known ~6-7x no-op amplification (one recrawl recently generated ~13k events). The daily heartbeat compounds any per-tick cost, so `recrawl` is kept OUT of the default worklist; debouncing the fan-out must land before `recrawl` is rotated into the unattended worklist. This is a change to the reactive core (its own invariant) — an ADR is required.

## Deferred

Tool synthesis · memory compilation · branch scoring · multi-worker scheduling · upstreaming lab conventions to activegraph-packs · PyPI packs.
