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
| `ACTIVEGRAPH_DB` | `data/lab.sqlite` | event-log store (resumed on restart) |
| `ACTIVEGRAPH_MEMORY_DB` | `data/lab_memory.sqlite` | memory_gateway backend |
| `LAB_LLM_PROVIDER` | auto | `openai` / `anthropic` / `mock` |
| `LAB_LLM_MODEL` | provider default | model override |

## Deploy (Replit, single lines)

- Push this repo to GitHub: `git push origin main`
- Import to Replit: create a Repl from the GitHub repo (the `.replit` run command is checked in)
- Add the Postgres integration (sets `DATABASE_URL` automatically)
- Generate the operator token: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- Set Secrets: `ANTHROPIC_API_KEY`, `LAB_OPERATOR_TOKEN` (from above), `LAB_ENV=prod` — leave `LAB_ALLOW_GRAPH_CODE` unset (ADR-012)
- Run. First boot seeds the mission (`mode=fresh`); redeploys resume (`mode=resumed`).

Verification (replace `$URL` and `$TOK`):

- Public read OK: `curl -s $URL/lab/feed | head -c 200`
- Backend is Postgres: `curl -s $URL/healthz | grep -o '"backend": "[a-z]*"'`
- Tokenless mutation refused: `curl -s -o /dev/null -w "%{http_code}\n" -X POST $URL/chat -d '{}'` → `401`
- Tokened mutation OK: `curl -s -X POST $URL/chat -H "Authorization: Bearer $TOK" -d '{"branch_id":"branch#2","content":"hello"}' | head -c 200`
- Restart the Repl, then: `curl -s $URL/healthz` → `event_count` ≥ before, boot log shows `mode=resumed`
- Postgres round-trip (scratch DB): `DATABASE_URL=... python scripts/test_postgres.py`

## Test suite (all keyless, all deterministic)

- Fixtures: `python lab_pack/fixtures/run_fixtures.py`
- Smoke (regression bar): `python scripts/smoke.py`
- Auth: `python scripts/test_auth.py`
- Public-safety sentinel audit: `python scripts/test_public_safety.py`
- UI render (jsdom, static fallback): `python scripts/check_ui.py`
