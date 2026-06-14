# INTERFACE

The UI is a projection of the event log — no state of its own (CONTRACT.md). Built fresh in `ui/`; the upstream Inspector is the separate debugging view (point it at the lab runtime — see README).

## Two surfaces (ADR-013)

The blog is the front door: `/` lists published posts (server-rendered from graph artifacts, never the drafts/ mirror), `/posts/<slug>` renders one post plus its "Show the work" provenance subgraph, `/feed.xml` is RSS. The notebook is the open workshop at `/lab`: ALL branches (open expanded; proposed/decided/archived collapsed), the inbox with resolved decisions in a collapsed history, a filter row (branch status, event kind, decision kind), and `#branch=<id>` deep links that post provenance points into. Published drafts in the feed cross-link their `/posts/<slug>` page. Both surfaces show the operator status line (`live|paused · $today/$cap`); the pause toggle renders in operator mode only (ADR-015).

## Thread = branch

One relation, not a new layer (ADR-004): a communication-pack `comm_thread` `discusses` a lab `branch`. Chatting in a thread is chatting inside the branch; the lab's `answer` behavior replies from graph state.

## Timeline (thread view)

Inside a thread: run events and chat messages interleaved in ONE scroll, ordered by the log. An input box posts to `/chat` with the thread→branch link. Pending decisions show approve/reject buttons that mutate the decision object through the API. No separate chat pane.

## Inbox resolution (ADR-026)

Approve/reject open an optional rationale field (skippable; recorded on the resolution event as `resolution_rationale`, `resolved_by=operator`). Pending decisions render their MCP annotations (`annotate_decision` — public, `operator_via_mcp`-attributed commentary, never authority); the rationale field prefills from the most recent annotation and the operator can edit before confirming. On resolve, annotations link into the decision's evidence. An open rationale form freezes its inbox block across re-renders (the 3s poll, SSE pushes, and the mobile keyboard's viewport churn): the textarea keeps its DOM node — typed text and focus — until explicit confirm or cancel.

## Feed

The home view: reverse-chron entries projected from lab events via `GET /lab/feed`, grouped by branch. Each entry is one human sentence derived from event type + payload — template-based; LLM narration is a later branch. Pending decisions are pinned at the top — that is the inbox, not a separate page.

## Branch listing

`GET /lab/branches?status=<proposed|active|decided|archived|all>` (and the MCP READ tool `list_branches`) is a read-only projection of branch state — one row per branch (id, title, status, authority, intent, pending-decision count), newest first, optionally filtered by status. It exists so proposed branches can be enumerated and activated (via a chat `activate` message) without hand-fetching ids from the UI. Pure projection, public; the HTTP and MCP views share one function so they cannot drift.

## Event-horizon stamps

Every answer is stamped with the last event it could see ("as of event N"). If work is in flight, the stamp tells the user how stale the answer might be.

## Fork rule

Forking anchors to a committed event only (`branch.fork_event_id`). In-flight work stays with the parent branch.

## Steering verbs

Inside a branch thread (word-boundary matched): `pause` → branch status `paused` (ADR-007); `resume` → `active` (only from paused); `activate` → `active` from proposed/scoped/decided/archived, recording the operator's rationale as an observation, resetting the dispatch dedup so a fresh task dispatches (carrying any operator_direction on the branch verbatim — ADR-027), and letting the existing dispatch react; archived → active is a deliberate operator resurrection, recorded as such; `deactivate` → back to `proposed` (ADR-025; both MCP-allowed — reversible, like pause); `draft` → operator draft request (ADR-014, bypasses the pending cap; the full message rides as the OPERATOR BRIEF and governs scope); `recrawl` → a fresh crawl request for the mission target_url; `propose <seam>` → seam proposal through the gate; `approve`/`reject` → resolves the branch's single pending decision — several pending lists the ids without mutating, zero is an honest no-op, and MCP-tagged messages are refused (the inbox is human-only, ADR-016/021). An ARCHIVED branch accepts exactly one steering verb — `activate`; every other verb draws an honest refusal naming it, and plain questions get an explicit archived notice.

Replies are composed AFTER the mutation, from post-mutation state, and cite the `lab.steering_applied` event id; an action no verb supports draws a refusal naming this verb set (ADR-025). The mutation lands at the event boundary.

## Deferred

Graph pane. Fork diff view. LLM-narrated feed entries. (Feed pagination closed: cursor on event id via /lab/entries, default page 100, 'load older'.)
