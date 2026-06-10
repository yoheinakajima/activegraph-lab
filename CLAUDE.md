# activegraph-lab

Self-hosted research agent built on ActiveGraph (event-sourced agent runtime, activegraph.ai). One agent, one mission: grow activegraph.ai's evidence base. One seed branch: `read_the_website`. The agent crawls the site, infers project claims, finds gaps, proposes branches, executes through worker capabilities, drafts artifacts, and requests approval before anything publishes or self-modifies. Every capability beyond the kernel arrives later through a gated branch. The UI is a notebook feed; each branch is a thread you can talk inside.

## Routing table

| Task type | Read first |
|---|---|
| Any code change | docs/CONTRACT.md |
| Pack structure, behaviors, object types, events, adapters | docs/ARCHITECTURE.md |
| UI, feed, threads, inbox, timeline | docs/INTERFACE.md |
| Why a thing is the way it is; changing an invariant | docs/DECISIONS.md |
| What to build next; scope questions | docs/ROADMAP.md |

## Build loop

1. Read docs/CONTRACT.md first, every session.
2. Check docs/DECISIONS.md before deviating from any prior choice.
3. Propose an ADR (and a decision object, once the runtime exists) before changing any invariant in CONTRACT.md.
4. Never grow scope beyond the current milestone in docs/ROADMAP.md. Deferred means deferred.

## Shell note

User shell is zsh. All shell commands in instructions to the user must be single-line.
