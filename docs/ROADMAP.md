# ROADMAP

The public answer to "what is this project doing next." Shipped is shipped; the queue is in order; deferred means deferred (moving anything up needs an ADR).

## Shipped

- Lab pack: three lab types over core, eleven reactive behaviors, deterministic keyless fixtures (ADR-001..008).
- Storage + deploy: native Postgres event store selected at boot, Replit always-on, public log policy with sentinel audit (ADR-009..011).
- Code residency: kernel/seams/graph-code/plumbing ladder; seams live and gated; graph code dark behind `LAB_ALLOW_GRAPH_CODE` (ADR-012).
- Public surface: the blog at `/` with per-post provenance, the workshop at `/lab`, editorial discipline (digest/research thresholds), pause + cost controls rebuilt from the log (ADR-013..015).
- MCP: read tools + send_chat with OAuth 2.1 for the Claude clients (ADR-016/017); chat-path failure domains, `/lab/errors`, bind-first readiness, sequence repair (ADR-023/024).
- This consolidation: the operator charter as seam `charter.mission` (ADR-018); per-behavior model routing behind a $100/day kernel ceiling (ADR-019); the lab-local research worker — research branches now execute (ADR-020); chat-triggered seam proposals through the gate (Phase 4 rails); MCP `get_log`/`get_entity` + reversible operator controls, the inbox still human-only (ADR-021); read-only GitHub tools, allowlisted (ADR-022 rung 1).

## In progress

- Live operation: charter-guided branch proposals and research-worker runs against activegraph.ai, accumulating rejections, findings, and drafts in the public log.

## The queue, in order

1. **Operator rejections → the lab's first self_modify voice proposal** (the reserved episode). The rails exist (Phase 4); the episode is the lab proposing a better `prompt.draft_writer` in its own voice, evidenced by the two rejected drafts and the operator's provenance questions already in the log. The file prose stays untouched until that decision is approved — the approval must be a recorded decision, not a commit.
2. **Charter-guided external portfolio**: VERIFY / BUILD / MEASURE branches per CHARTER v1 — site claims tested empirically, demos in the AG Coder spirit, benchmark experiments in the Regimes spirit. Self-referential work stays a minority of open branches.
3. **The fork branch (flagship demo)**: verify "replay, fork, and diff any run" by forking one of the lab's own branches and diffing the outcomes — the claim, tested on the lab itself, with the diff as the artifact.
4. **Graph-code enablement** — `LAB_ALLOW_GRAPH_CODE=1` is set only when ALL of: an approved behavior draft exists that the operator actually wants live; the sandbox pipeline has rejected at least one draft for cause (the gate has demonstrated teeth); a rollback drill has been run (flag off → behavior gone, log intact); and the watchdog covers graph-code behaviors like any other.
5. **GitHub rung 2 (`submit_pr`, ADR-022)** — built only when ALL of: rung-1 reads have produced evidence in published work; the graph-code sandbox pipeline is live (criterion 4); a separate write-scoped token exists, absent from deploys until then; and the two-gate flow (approved submit_pr decision AND operator review of the PR on GitHub) is fixture-locked before any token is configured.

## Deferred

Tool synthesis · memory compilation · branch scoring · multi-worker scheduling · upstreaming lab conventions to activegraph-packs · PyPI packs.
