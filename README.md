# activegraph-lab

Self-hosted research agent built on [ActiveGraph](https://activegraph.ai) (event-sourced agent runtime). One agent, one mission: grow activegraph.ai's evidence base. One seed branch: `read_the_website`. The agent crawls the site, infers project claims, finds gaps, proposes branches, dispatches work, drafts artifacts, and requests approval before anything publishes or self-modifies. The UI is a notebook feed; each branch is a thread you can talk inside.

Built as a layered pack on [activegraph-packs](https://github.com/yoheinakajima/activegraph-packs) (consumed as a pinned git dependency — ADR-005). Governing docs: start at [CLAUDE.md](CLAUDE.md) and [docs/CONTRACT.md](docs/CONTRACT.md).

## Layout

```
lab_pack/    the lab as one pack: mission/branch/decision + six behaviors
  bundle.py  build_lab() — composes upstream packs + lab_pack, seeds branch zero
  fixtures/  deterministic scenarios, no API key, no network
server/      thin HTTP server: /lab/feed /chat /lab/decision /graph /trace /reset
ui/          the notebook feed (vanilla JS, served by the server at /)
docs/        CONTRACT, ARCHITECTURE, INTERFACE, DECISIONS, ROADMAP
data/        SQLite event log + memory store (created at first run)
```

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

Open the notebook feed:

```sh
open http://localhost:7799/
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
