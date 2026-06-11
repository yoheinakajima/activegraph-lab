# activegraph-lab on Replit

Self-hosted research agent on ActiveGraph: one mission (grow
activegraph.ai's evidence base), branches as threads, everything gated.
Continuously running server; all durable state in managed Postgres
(ADR-009/010); the event log, feed, drafts, and operator chat are public
(ADR-011) — mutations need the operator token.

## Install — use uv, not Replit's package installer

Replit's package installer segfaults on this project (observed on deploy;
the pinned git dependency + extras trip it). Install with uv into the
managed environment instead — single line:

    uv pip install -e .

If uv is missing, first run: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
Do not "fix" a failed deploy by re-running the Replit installer — it
rediscovers the segfault every time.

Stock uv install works as of 0d35d34 — the activegraph-packs git pin is
committed in `[tool.uv.sources]`, so no local pyproject patching is
needed in this environment anymore.

## Surface map

| Route | What |
|---|---|
| `/` | the public blog (SSR from the graph; ADR-013) |
| `/posts/<slug>` | one post + its "Show the work" provenance subgraph |
| `/feed.xml` | RSS, published posts only |
| `/lab` | the open-workshop notebook (everything, live) |
| `/healthz` | backend, events, paused, calls/cost vs caps |
| `POST /lab/pause` / `POST /lab/resume` | operator token; global pause (ADR-015) |
| `POST /mcp` | MCP server, streamable HTTP (ADR-016); auth: OAuth bearer (ADR-017), legacy `LAB_MCP_TOKEN` bearer, or `/mcp/<LAB_MCP_TOKEN>` path token — identical authority in all three; read tools + `send_chat`, never decisions or pause |
| `/.well-known/oauth-*`, `POST /register`, `GET+POST /authorize`, `POST /token` | OAuth 2.1 + DCR for claude.ai's connector (ADR-017) — STATELESS: tokens are HMAC-signed payloads keyed from `LAB_MCP_TOKEN`, verified by recomputation; the operator pastes `LAB_MCP_TOKEN` once on the `/authorize` page |

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
| `LAB_MCP_TOKEN` | the one secret behind `/mcp` (ADR-016/017): legacy bearer, the OAuth signing root, AND the password the operator pastes on `/authorize`; permits MCP reads + `send_chat` but NEVER decisions or pause, and the operator token never opens `/mcp`; unset → MCP + OAuth disabled; rotating it revokes every OAuth client/token at once (nothing is stored) |
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
- LLM spend: 5 calls/behavior-run, 60/session, 200/day, $5.00/day cost
  ceiling (UTC reset, counted/summed from the event log — restart-proof;
  ADR-015). The global pause is also an event: bouncing the process resets
  neither.

## Verify a deploy

See README.md → Deploy for the curl checklist; the one-shot Postgres
round-trip test is `python scripts/test_postgres.py` (uses DATABASE_URL,
needs an empty scratch database).
