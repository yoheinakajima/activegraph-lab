# activegraph-lab on Replit

Self-hosted research agent on ActiveGraph: one mission (grow
activegraph.ai's evidence base), branches as threads, everything gated.
Continuously running server; all durable state in managed Postgres
(ADR-009/010); the event log, feed, drafts, and operator chat are public
(ADR-011) — mutations need the operator token.

## Run

The run command is `python server/lab_server.py` (binds 0.0.0.0, honors
`PORT`). First boot with an empty database seeds the mission and branch
zero; every later boot resumes from the event log (`mode=resumed` in the
boot log).

## Secrets (Replit Secrets pane — never the filesystem, never the graph)

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | live LLM behaviors (absent → deterministic mock mode) |
| `LAB_OPERATOR_TOKEN` | bearer token for mutations; unset → read-only mode |
| `LAB_ENV` | `prod` (disables /reset; set by .replit) |
| `DATABASE_URL` | added automatically by the Replit Postgres integration |

`LAB_ALLOW_GRAPH_CODE` is **intentionally absent**: approved graph-code
drafts stay dormant (ADR-012). Flipping it on is a deliberate operator
act, not a deploy default.

## Invariants that matter here

- The gate is absolute: nothing publishes or self-modifies without an
  approved decision (and graph code additionally needs the flag).
- No secrets in any event payload, log line, or error path — enforced by
  `python scripts/test_public_safety.py`.
- LLM spend: 5 calls/behavior-run, 60/session, 200/day (UTC reset,
  counted from the event log — restart-proof).

## Verify a deploy

See README.md → Deploy for the curl checklist; the one-shot Postgres
round-trip test is `python scripts/test_postgres.py` (uses DATABASE_URL,
needs an empty scratch database).
