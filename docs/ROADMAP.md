# ROADMAP

Build only the current milestone. Deferred items need an ADR to move up.

## Milestone 0: doc scaffold (this session)

Repo skeleton, the six governing docs. Done when this file exists.

## Milestone 1: lab_kernel

The pack: five object types, ingest behavior. Branch zero (`read_the_website`) runs against activegraph.ai and produces a gap list as evidence objects. No UI, no workers — kernel behaviors only. Done when the gap list exists in the graph and replays cleanly from the log.

## Milestone 2: lab_interface

Message ingestion and the answer behavior. Done when you can talk to branch zero: send a message, get a stamped answer that reflects graph state without blocking on running work.

## Milestone 3: first worker adapter

`deep-research` adapter behind the worker contract (progress events or declared-uninterruptible). Done when a branch can dispatch a research task and the results land as evidence objects.

## Deferred

- Tool synthesis
- Behavior drafting
- Memory compilation
- Branch scoring
- Multi-worker scheduling
- Public site
