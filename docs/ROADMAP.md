# ROADMAP

Build only the current milestone. Deferred items need an ADR to move up.

## Milestone 0: doc scaffold — done

Repo skeleton, the six governing docs, ADR-003..006 amendments.

## Milestone 1: lab_pack — current

The single lab pack per ADR-003/004: three object types, six behaviors (ingest, plan, work, interpret, gate, answer), deterministic fixtures runnable without an API key (`python lab_pack/fixtures/run_fixtures.py`). Done when all fixtures pass.

## Milestone 2: bundle + thin server

`build_lab()` composing upstream packs + lab_pack, creating the activegraph.ai mission and the `read_the_website` seed branch. Thin server with `/graph`, `/trace`, `/chat`, `/reset`, `GET /lab/feed`. Done when you can talk to branch zero through `/chat` and read the feed projection.

## Milestone 3: notebook feed UI

`ui/`: feed view (reverse-chron, branch-grouped, pinned pending decisions) and thread view (one-scroll timeline, chat input, approve/reject). Done when a gated decision can be approved from the browser.

## Milestone 4: live run

Branch zero runs against https://activegraph.ai with a real LLM provider and real fetches through tool_gateway, producing a gap list as observation objects that replays cleanly from the log.

## Milestone 5: public surface, editorial policy, operator controls — done

ADR-013..015: the blog at `/` (SSR from the graph, provenance on every post, RSS), the open workshop at `/lab`, the digest/research editorial discipline with seam-tunable thresholds, the global pause and the daily cost ceiling rebuilt from the log.

## Deferred

- Tool synthesis
- Behavior drafting
- Memory compilation
- Branch scoring
- Multi-worker scheduling
- Upstream the lab pack or its conventions to activegraph-packs if it proves general
- Consume packs from PyPI once published
