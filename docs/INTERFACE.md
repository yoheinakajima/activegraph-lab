# INTERFACE

Outline. The UI is a projection of the event log — no state of its own (CONTRACT.md). Built starting Milestone 2.

## Thread = branch = runtime fork

One concept at three layers. A conversation thread in the UI is a branch object in the graph is a fork in the runtime. There is no separate chat model.

## Timeline

Inside a thread: run events and messages interleaved in one scroll, ordered by the log. The user reads what the agent did and says things into the same stream. No separate "activity" tab.

## Feed

The home view: all branches, notebook register. Entries are agent-narrated — the agent writes what happened in each branch, in prose, as it happens. OPEN: narration cadence (per event, per work cycle, or on-demand summarization).

## Inbox

Gated decisions awaiting the user, each with its linked evidence attached inline. Approving or rejecting writes a decision object and its gate event. The inbox is the only place anything blocks on the user.

## Event-horizon stamps

Every answer from the fast answer behavior is stamped with the last event it could see. If work is in flight, the stamp tells the user how stale the answer might be.

## Fork rule

Forking a thread anchors to a committed event only; the picker offers committed events, nothing in-flight. In-flight work stays with the parent branch.

## Deferred

Graph pane (visual object graph). Diff view (compare branches or artifact versions). Not in any current milestone.
