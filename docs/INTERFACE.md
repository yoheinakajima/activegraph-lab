# INTERFACE

The UI is a projection of the event log — no state of its own (CONTRACT.md). Built fresh in `ui/`; the upstream Inspector is the separate debugging view (point it at the lab runtime — see README).

## Two surfaces (ADR-013)

The blog is the front door: `/` lists published posts (server-rendered from graph artifacts, never the drafts/ mirror), `/posts/<slug>` renders one post plus its "Show the work" provenance subgraph, `/feed.xml` is RSS. The notebook is the open workshop at `/lab`: ALL branches (open expanded; proposed/decided/archived collapsed), the inbox with resolved decisions in a collapsed history, a filter row (branch status, event kind, decision kind), and `#branch=<id>` deep links that post provenance points into. Published drafts in the feed cross-link their `/posts/<slug>` page. Both surfaces show the operator status line (`live|paused · $today/$cap`); the pause toggle renders in operator mode only (ADR-015).

## Thread = branch

One relation, not a new layer (ADR-004): a communication-pack `comm_thread` `discusses` a lab `branch`. Chatting in a thread is chatting inside the branch; the lab's `answer` behavior replies from graph state.

## Timeline (thread view)

Inside a thread: run events and chat messages interleaved in ONE scroll, ordered by the log. An input box posts to `/chat` with the thread→branch link. Pending decisions show approve/reject buttons that mutate the decision object through the API. No separate chat pane.

## Feed

The home view: reverse-chron entries projected from lab events via `GET /lab/feed`, grouped by branch. Each entry is one human sentence derived from event type + payload — template-based; LLM narration is a later branch. Pending decisions are pinned at the top — that is the inbox, not a separate page.

## Event-horizon stamps

Every answer is stamped with the last event it could see ("as of event N"). If work is in flight, the stamp tells the user how stale the answer might be.

## Fork rule

Forking anchors to a committed event only (`branch.fork_event_id`). In-flight work stays with the parent branch.

## Steering verbs

Inside a branch thread: `pause` → branch status `paused` (ADR-007); `resume` → `active` (only from paused); `approve`/`reject` → resolves the branch's pending decision. The reply confirms; the mutation lands at the event boundary.

## Deferred

Graph pane. Fork diff view. LLM-narrated feed entries. (Feed pagination closed: cursor on event id via /lab/entries, default page 100, 'load older'.)
