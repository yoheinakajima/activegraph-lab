# activegraph-lab

Self-hosted research agent built on [ActiveGraph](https://activegraph.ai) (event-sourced agent runtime). One agent, one mission: grow activegraph.ai's evidence base. One seed branch: `read_the_website`. The agent crawls the site, infers project claims, finds gaps, proposes branches, dispatches work, drafts artifacts, and requests approval before anything publishes or self-modifies. The UI is a notebook feed; each branch is a thread you can talk inside.

Built as a layered pack on [activegraph-packs](https://github.com/yoheinakajima/activegraph-packs) (consumed as a pinned git dependency — ADR-005). Governing docs: start at [CLAUDE.md](CLAUDE.md) and [docs/CONTRACT.md](docs/CONTRACT.md).

## Layout

```
lab_pack/    the lab as one pack: mission/branch/decision + eight behaviors
  bundle.py  build_lab() — composes upstream packs + lab_pack, seeds branch zero
  fixtures/  deterministic scenarios, no API key, no network
server/      thin HTTP server + the public blog (server/blog.py, SSR)
ui/          the notebook feed (vanilla JS, served by the server at /lab)
docs/        CONTRACT, ARCHITECTURE, INTERFACE, DECISIONS, ROADMAP
data/        SQLite event log + memory store (created at first run)
```

## Surface map (ADR-013)

| Route | What |
|---|---|
| `/` | the public blog — published posts, server-rendered from the graph, newest first |
| `/posts/<slug>` | one post + "Show the work": branch, evidence, chat, the publish decision, prior drafts |
| `/feed.xml` | RSS 2.0, published posts only |
| `/lab` | the open workshop: every branch (incl. proposed/decided/archived), every chat, every decision, filter row, deep links via `#branch=<id>` |
| `/healthz` | backend, event count, pending decisions, paused, LLM calls/cost vs caps |
| `POST /mcp` | MCP server, streamable HTTP (ADR-016/021/026) — read tools, `send_chat`, reversible controls (budget, pause/resume), decision annotations; never approve/reject or seam promotion |
| `/.well-known/oauth-*`, `/register`, `/authorize`, `/token` | OAuth 2.1 + DCR for the MCP surface (ADR-017), stateless |

## MCP surface + claude.ai connector (ADR-016/017)

`/mcp` accepts three equivalent credentials, all rooted in `LAB_MCP_TOKEN` (a separate secret from the operator token, revocable independently): an OAuth 2.1 bearer token, the legacy `Authorization: Bearer $LAB_MCP_TOKEN` header, or the legacy `/mcp/<LAB_MCP_TOKEN>` path token. Identical authority in all three — read tools, `send_chat`, the reversible operator controls (`set_budget`, `pause_lab`/`resume_lab` — ADR-021), and `annotate_decision` (public pre-review commentary on a pending decision — ADR-026); approving/rejecting decisions and seam promotion are excluded by design (the inbox is the one place only the human operator exists).

The OAuth server is **stateless**: client ids, codes, and access/refresh tokens are HMAC-signed payloads keyed from `LAB_MCP_TOKEN`, verified by recomputation — nothing is stored, so rotating `LAB_MCP_TOKEN` revokes everything at once.

**Connect claude.ai** (web/desktop — Settings → Connectors → Add custom connector):

1. Enter the plain MCP URL: `https://<your-host>/mcp` — no token in it.
2. Click Connect. claude.ai discovers the OAuth metadata, registers itself, and opens the lab's `/authorize` page.
3. Paste `LAB_MCP_TOKEN` into the password field once. You're redirected back and the connector is live (24h access tokens, refreshed automatically for 30 days).

Verification (zsh, single lines — replace `$URL`):

- Metadata: `curl -s $URL/.well-known/oauth-authorization-server | python3 -m json.tool`
- Resource metadata: `curl -s $URL/.well-known/oauth-protected-resource/mcp | python3 -m json.tool`
- Discovery challenge: `curl -s -i -X POST $URL/mcp -d '{}' | grep -i www-authenticate` → `Bearer resource_metadata=...`
- DCR (deterministic client_id): `curl -s -X POST $URL/register -H 'Content-Type: application/json' -d '{"redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}'`
- Bad grant is a fixed body: `curl -s -X POST $URL/token -d 'grant_type=authorization_code&code=x&code_verifier=y&client_id=z&redirect_uri=w'` → `{"error": "invalid_grant"}`

## Operator controls (ADR-015)

All mutations need `Authorization: Bearer $LAB_OPERATOR_TOKEN`.

- `POST /lab/pause` / `POST /lab/resume` — global pause, persisted as `lab.paused`/`lab.resumed` events and rebuilt from the log at boot. While paused every LLM behavior except `answer` idles (one behavior-skipped observation per behavior per episode); the operator can always talk to the lab.
- Daily cost ceiling: `setting.daily_cost_cap_usd` (default $5.00, seam-eligible) over activegraph's native `cost_usd` accounting on `llm.responded` events — restart-proof, like the daily call cap.
- Editorial policy thresholds (`digest_min_findings`, `research_min_evidence`, `max_drafts_pending`) are seam settings: tuning them is self-modification through the gate (ADR-014).
- The UI shows `live|paused · $today/$cap` in the `/lab` header and the blog footer; the pause toggle appears in operator mode only.

## Quickstart (zsh, single lines)

Install:

```sh
pip install -e .
```

Run the fixture suite (no API key, no network):

```sh
python lab_pack/fixtures/run_fixtures.py
```

Start the lab (mock LLM unless OPENAI_API_KEY / ANTHROPIC_API_KEY is set):

```sh
python server/lab_server.py
```

Open the blog (front door) and the notebook (open workshop):

```sh
open http://localhost:7799/ http://localhost:7799/lab
```

Local-dev override for the packs dependency (instead of the pinned SHA):

```sh
pip install -e ../activegraph-packs
```

## Debugging view

The upstream Inspector remains the debugging view — point it at the lab runtime (the lab serves the Inspector's `/graph`, `/trace`, `/summary`, `/packs` endpoints):

```sh
cd ../activegraph-packs && ACTIVEGRAPH_PORT=7799 pnpm dev
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `LAB_PORT` | `7799` | server port |
| `LAB_DATABASE_URL` | unset | Postgres event store; wins over `DATABASE_URL` (ADR-009 note). In production this is the Replit-managed Neon cluster's writable primary endpoint |
| `DATABASE_URL` | unset | fallback Postgres URL (Replit reserves this name — see replit.md) |
| `ACTIVEGRAPH_DB` | `data/lab.sqlite` | SQLite event-log store when no Postgres URL is set (resumed on restart) |
| `ACTIVEGRAPH_MEMORY_DB` | `data/lab_memory.sqlite` | memory_gateway backend |
| `LAB_LLM_PROVIDER` | auto | `openai` / `anthropic` / `mock` |
| `LAB_LLM_MODEL` | provider default | model override |

## Deploy (Replit, single lines)

- Push this repo to GitHub: `git push origin main`
- Import to Replit: create a Repl from the GitHub repo (the `.replit` run command is checked in)
- Add the Postgres integration (`postgresql-16` module — it owns the Replit-managed Neon cluster that is the production database; do NOT remove it)
- Supply the cluster's **writable primary** endpoint URL as the `LAB_DATABASE_URL` secret — Replit reserves the name `DATABASE_URL` at publish time, so the lab reads `LAB_DATABASE_URL` first and falls back to `DATABASE_URL` (ADR-009 note)
- Generate the operator token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- Set Secrets: `ANTHROPIC_API_KEY`, `LAB_OPERATOR_TOKEN` (from above), `LAB_ENV=prod` — leave `LAB_ALLOW_GRAPH_CODE` unset (ADR-012)
- Run. First boot seeds the mission (`mode=fresh`); redeploys resume (`mode=resumed`).

Verification (replace `$URL` and `$TOK`):

- Public read OK: `curl -s $URL/lab/feed | head -c 200`
- Backend is Postgres: `curl -s $URL/healthz | grep -o '"backend": "[a-z]*"'`
- Tokenless mutation refused: `curl -s -o /dev/null -w "%{http_code}\n" -X POST $URL/chat -d '{}'` → `401`
- Tokened mutation OK: `curl -s -X POST $URL/chat -H "Authorization: Bearer $TOK" -d '{"branch_id":"branch#2","content":"hello"}' | head -c 200`
- Restart the Repl, then: `curl -s $URL/healthz` → `event_count` ≥ before, boot log shows `mode=resumed`
- Postgres round-trip (scratch DB): `LAB_DATABASE_URL=... python scripts/test_postgres.py`

## Test suite (all keyless, all deterministic)

- Fixtures: `python lab_pack/fixtures/run_fixtures.py`
- Smoke (regression bar): `python scripts/smoke.py`
- Auth: `python scripts/test_auth.py`
- MCP surface: `python scripts/test_mcp.py`
- OAuth 2.1 + DCR (ADR-017): `python scripts/test_oauth.py`
- Public-safety sentinel audit: `python scripts/test_public_safety.py`
- Chat-path robustness (ADR-023; add `LAB_TEST_PG_URL=...scratch...` for the real leaf): `python scripts/test_chat_robustness.py`
- Boot readiness (ADR-024): `python scripts/test_readiness.py`
- UI render (jsdom, static fallback): `python scripts/check_ui.py`
