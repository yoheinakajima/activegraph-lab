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

## In progress

- Live operation: charter-guided branch proposals and research-worker runs against activegraph.ai, accumulating rejections, findings, and drafts in the public log.

## The queue, in order

1. **Operator rejections → the lab's first self_modify voice proposal** (the reserved episode). The rails exist (Phase 4); the episode is the lab proposing a better `prompt.draft_writer` in its own voice, evidenced by the two rejected drafts and the operator's provenance questions already in the log. The file prose stays untouched until that decision is approved — the approval must be a recorded decision, not a commit.
2. **Charter-guided external portfolio**: VERIFY / BUILD / MEASURE branches per CHARTER v1 — site claims tested empirically, demos in the AG Coder spirit, benchmark experiments in the Regimes spirit. Self-referential work stays a minority of open branches.
3. **The fork branch (flagship demo)**: verify "replay, fork, and diff any run" by forking one of the lab's own branches and diffing the outcomes — the claim, tested on the lab itself, with the diff as the artifact.
4. **Graph-code enablement** — `LAB_ALLOW_GRAPH_CODE=1` is set only when ALL of: an approved behavior draft exists that the operator actually wants live; the sandbox pipeline has rejected at least one draft for cause (the gate has demonstrated teeth); a rollback drill has been run (flag off → behavior gone, log intact); and the watchdog covers graph-code behaviors like any other.
5. **GitHub rung 2 (`submit_pr`)** — BUILT (ADR-035). The rails exist and are fixture-locked: the repo sandbox (secret-isolation sentinel-gated), the code worker on `codebase.code_task`, and the doubly-gated submit_pr decision. The first REAL exercise is operator-chosen: configure `GITHUB_WRITE_TOKEN` (fine-grained, lab repo only) and approve a submit_pr the lab opened. Until then the lab drafts + sandbox-proves + proposes, and the operator opens the PR by hand from the diff.

## The self-repair loop (ADR-035, the rung this session built)

The path from a production bug to a merged fix, end to end — wired and proven on the rails this session, NOT auto-fired:

1. A production failure surfaces as a **finding** observation (the diagnostics ring buffer / a chat erratum / a verification branch's verdict).
2. The lab proposes a **fix**: a `code_task` in the codebase lane carrying the repo, a test/build command, and a proposed diff.
3. The **code worker** clones the allowlisted repo into the **repo sandbox**, runs the command (baseline), applies the diff, and re-runs to **prove** it — captured output becomes attributed evidence, a `sandbox_proven` evaluation links to the branch.
4. The lab opens a **submit_pr** decision (the diff + the sandbox proof), status pending — the inbox.
5. The **operator approves** (the first gate) → the lab exercises `GITHUB_WRITE_TOKEN` to push the branch and open the **PR**.
6. The **operator reviews and merges** on GitHub (the second gate, a different system) — no auto-merge.
7. **Replit pulls** the merged main on its next deploy.

Worked example (the one this session was triggered by): branch#847 — "Fetch the actual implementation files … get_file on the event-log projection, replay, fork, and diff code paths … confirm the claims hold at the level of code." Its action is READ-TO-VERIFY, so origin/main's verb classifier correctly routes it `research.deep_research` (which carries get_file) — the production misroute to `codebase.code_task` was a deployment REGRESSION (a rebase reverted the router to the keyword version), not a classifier bug. The fix this session adds is twofold: a routing fixture locks branch#847's exact intent to research (so the regression fails CI loudly), AND the new code worker makes the branch answerable EITHER way — if a verification task still reaches the codebase lane, the worker records a `routing_miss` naming research and fails with an actionable verdict rather than dying in a dead lane. Verb-safe re-dispatch message after deploy: *"Re-verify the replay/fork/diff claims by reading the activegraph source — fetch the event-log projection and replay code paths and check the claims against them."* (fetch/read/check — no build/implement/fix verb, so it routes research.deep_research and the research worker's get_file fetches the files).

## Deferred

Tool synthesis · memory compilation · branch scoring · multi-worker scheduling · upstreaming lab conventions to activegraph-packs · PyPI packs.
