# HANDOFF — consolidation session (claude/activegraph-lab-consolidation-3geq7i)

Bail-out state tracker for the six-phase "prose rounds up, graph tells truth"
consolidation. Phases run in order; suites must be green after each. Delete
this file once all phases land and the final summary is written.

## Suite commands (all keyless, deterministic, no live LLM)
- `python lab_pack/fixtures/run_fixtures.py`
- `python scripts/smoke.py` · `test_auth.py` · `test_mcp.py` · `test_oauth.py`
- `python scripts/test_public_safety.py` · `test_chat_robustness.py` · `test_readiness.py`
- `python scripts/check_ui.py`

Setup: `uv pip install --system -e .` (plain pip can't resolve activegraph-packs).

## Baseline (start of session): ALL GREEN — 29/29 fixtures + 8 script suites.

## Phase status
- [x] PHASE 1 — overclaim lint in drafts. ADR-033. `_overclaim_review` in
      behaviors.py (sibling to `_coverage_review`), wired into draft_writer.
      Fixture: draft_writer Phase 1. GREEN.
- [x] PHASE 2 — MCP send_chat commit-and-return. ADR-034 (amends ADR-016/023).
      _send_chat returns status=accepted immediately + fire-and-forget reply
      via _submit_to_worker; removed _reply_wait_seconds. mcp_reply_wait_seconds
      RETIRED IN PLACE (no-op; kernel whitelist NOT edited — kept untouchable).
      Updated test_mcp, test_chat_robustness, test_oauth. GREEN.
- [ ] PHASE 3 — pin "accident became policy" properties: (a) daily budget cap
      rebuilds across restart; (b) seam cannot activate except via gate-approved
      hot-load. Named regression tests. Seed a keyed finding. REPORT if a
      property does NOT hold.
- [ ] PHASE 4 — operator_note/annotate reachability over MCP (finish ADR-028).
      Verify bare branch-annotate over MCP exists; add if not (operator-tier,
      records operator_note observation, no status change).
- [ ] PHASE 5 — cadence metadata: one-line "evidence profile" in each draft's
      review note (own-live vs inherited findings; distinct-branch span).
      No auto-block.
- [ ] PHASE 6 — _EXISTING_CAPABILITY_ALIASES maintenance guard test: every
      tool in RESEARCH_WORKER_TOOLS has alias-map coverage or documented
      exemption. No runtime change.

## Hard rules
- Kernel untouchable (lab_pack/kernel.py manifest).
- Do NOT edit promoted graph versions (charter v2, draft_writer v1 live
  self-mods) — file defaults only.
- No new dependencies. Any deviation → ADR + stop.
- ADR for any phase moving a documented invariant (expect Phase 1 + Phase 2).

## Redeploy steps
Pure code/test changes; standard Replit redeploy (git push → resume boot).
No new secrets, no schema changes, no settings migration except Phase 2's
removal/repurposing of `mcp_reply_wait_seconds`. Detail per phase in the
final summary.
