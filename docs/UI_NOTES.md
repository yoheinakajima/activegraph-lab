# UI_NOTES — universal navigability audit & design

Phase A analysis for the "every id is a link, every entity inspectable" upgrade.
Scope fences: `ui/*`, `server/blog.py` HTML, plus read-only GET projections in
`server/lab_server.py`. Nothing behind the gate, in `lab_pack/`, or in auth moves.

## 1. Inventory — what the system actually produces

### Id grammar

- Objects: `type#N` — one shared counter across all types (`mission#1`, `branch#2`, `observation#3`).
- Events: `evt_NNN` — zero-padded, 1-based, strictly ordered (`_event_index` parses the int).
- Relations: `rel_NNN` (ride inside `relation.created` payloads; not addressable via `get_object`).

### Event kinds (observed in a live run; runtime owns the taxonomy)

| kind | payload shape | feed today |
|---|---|---|
| `object.created` | `{object:{id,type,data}, id}` | narrated iff lab-relevant |
| `patch.applied` | `{patch, target, diff}` | narrated for status changes |
| `relation.created` | `{relation:{id,source,target,type,provenance}, …}` | invisible |
| `behavior.started` | `{behavior, event_id, triggering_event_type, triggering_object_id}` | invisible (~40% of the log) |
| `behavior.completed` | `{behavior, objects_created, relations_created, patches_applied, …}` | invisible (~40% of the log) |
| `llm.requested` | `{behavior, model, prompt, prompt_hash, …}` | invisible |
| `llm.responded` | `{raw_text, parsed, input/output_tokens, cost_usd, …}` | invisible (the cost data!) |
| `pack.loaded` | `{name, version, object_types, behaviors, …}` | invisible |
| `runtime.idle` | `{snapshot}` | invisible |
| `artifact.published` (marker) | `{slug, title, artifact_id}` | narrated |
| `lab.paused` / `lab.resumed` (marker) | `{by}` | narrated |

In a mock end-to-end run, narrated entries cover ~8% of events. The rest is
reachable only via raw `/trace` JSON.

### Object types in the composed graph

- **Lab (3):** `mission`, `branch` (note `parent_branch_id`, `fork_event_id`,
  `authority` — never rendered), `decision` (`subject_ref`, `kind`, `evidence_refs`).
- **Core, lab-used:** `artifact` (kind: `blog_draft`, `seam`, `upstream_issue`,
  `behavior_draft`, `tool_draft`), `observation` (metadata.lab kinds: `site_claim`,
  `capability_gap`, `interpretation`, `fetch_failure`, `stall`, `llm_budget`,
  `llm_parse_failure`, `finding`, `upstream_friction`, `draft_mirror_failure`,
  `synthetic_crawl`, `seam_refused`, `draft_request`, `drafting_idle`,
  `behavior_skipped`, `gate_violation`, `sandbox`, `graph_code`, `graph_code_check`),
  `evaluation` (`task_outcome`, graph-code pipeline checks), `task`
  (`metadata.routing`), `source` (`tool_result`, `crawl_request`).
- **Upstream packs:** `comm_thread`, `comm_message`, `comm_intent`,
  `comm_participant`, `comm_response_candidate`, `memory_candidate`, `memory_item`,
  `principal`, `auth_context`, `capability_provider/call/approval/result`.

### Relation types (decoded via `compat.decode_relation`)

Lab: `has_branch`, `forked_from`, `produced`, `supported_by`, `dispatched`,
`discusses`, plus `queued_finding`, `covers` (digest/editorial).
Upstream: `proposes`, `evaluates`, `accepted_as`, `grounds`, `resolves_to`,
`authenticated_by`, `calls`, `approved_by`, `produces_result`, `sourced_as`,
`intent_of`, `thread_contains`, `response_to`, `dispatched_to`.
No UI surface renders ANY relation today (the blog provenance section is a
hand-built join, not relation traversal).

### Existing read projections

`/lab/feed`, `/lab/entries` (cursor), `/lab/stream` (SSE), `/lab/draft?slug=`,
`/lab/seams`, `/graph` (all objects + decoded relations), `/trace` (all events,
offset/limit, full payloads), `/summary`, `/packs`, `/healthz`; blog `/`,
`/posts/<slug>`, `/feed.xml`.

## 2. Gap list — shown / dead-ended / unreachable

**Shown:** narrated feed entries grouped by branch; inbox + resolved decisions;
branch threads with chat; seams table; blog posts with provenance text.

**Dead-ended (id visible, not clickable):**
- every entry's `evt_*` chip (rendered as plain text on every row)
- decision cards: `subject_ref` not shown as link, evidence items show bare ids
- resolved decision rows: no link to the decision, subject, or branch
- blog "Show the work": evidence ids, decision id, chat — plain text; only the
  branch links (to the thread, not to the branch object)
- seams view: pending `decision#N` ids plain text
- the lab's own chat answers cite `evt_N` / object ids in prose — dead text

**Unreachable entirely (no click path from any page):**
- any single object's full fields (observation text is truncated everywhere;
  task routing, source contents, artifact metadata, decision evidence_refs)
- raw event JSON for anything
- relations in/out of anything
- ~92% of the event log (`behavior.*`, `llm.*`, `relation.created`,
  `pack.loaded`, `runtime.idle`)
- branch lineage (`parent_branch_id`, `fork_event_id`)
- upstream-pack objects (threads, intents, memory, capability calls)
- prev/next walking of the log around an event

## 3. Design

### New projection endpoints (the only `lab_server.py` changes)

Both are pure functions of `graph.events` / `graph.objects` / `graph.relations`
— no state, no writes, no caches. Their outputs join the sentinel audit corpus.

1. **`GET /lab/entity?id=<id>`** — the universal inspector projection.
   - Object id (`type#N`): the object (id/type/full data), a display title, the
     creation event (id/actor/timestamp), all `patch.applied` events that touched
     it, relations in and out (decoded, each with the other end's type+title),
     the feed narration if any, and artifact extras (slug, published, post URL).
   - Event id (`evt_N`): the full event dict, `prev_id`/`next_id`/`index`/`total`
     (place in time), a one-line summary, and every object id referenced by the
     payload (deduped) so the event links onward.
   - Unknown id → 404 JSON.
2. **`GET /lab/log?before=<evt_id>&limit=N`** — the FULL log as lightweight rows
   `{event_id, ts, actor, event_type, summary, branch_id?}`, cursor-paginated
   like `/lab/entries`. Exists because `/trace` returns complete payloads
   (LLM prompts included) — megabytes on a phone for a listing view. Every event
   kind gets a one-line summary; unknown kinds render as the kind name (never
   blank).

### UI (vanilla JS/CSS, hash-routed, all deep-linkable)

- **Linkify everywhere:** any `evt_N` / `type#N` token rendered in a sentence,
  card, or table becomes `<a href="#entity=…">`. Entry event-id chips link too.
- **Entity view (`#entity=<id>`):** header (type chip, id, title), narration,
  fields table (ids inside values are links; nested data pretty-printed), the
  provenance line ("created by evt_N by actor, when" + patch list), relations
  in/out as link lists, raw JSON in a collapsed `<details>`, type-specific
  affordances (branch → "open thread", artifact → post/draft link, decision →
  subject, event → prev/next nav + refs).
- **Log view (`#log`):** the whole event log, newest first, "load older", every
  row clickable. The feed shows the story; the log shows the record.
- **Blog provenance as navigation (`server/blog.py`):** every id in "Show the
  work" links to `/lab#entity=<id>`; the post header links its artifact's
  entity page; entity pages of published artifacts link back to `/posts/<slug>`.
  Walking post → decision → evidence → branch → events → back works by click.
- **Orientation:** a collapsed "What is this?" strip at the top of /lab —
  four sentences (mission, branches=threads, inbox=human gate, everything is an
  event; ids are clickable) plus a chip legend. No tour, no modal.
- **Seams deep link (`#seams`)** and linkified pending decision ids.
- **Mobile:** filter row and header meta wrap; entity tables and `<pre>` scroll
  horizontally; all views verified at 390px.
- **check_ui:** extended to render the entity view (object + event), the log
  view, assert linkified ids in the feed, prev/next presence, and that no log
  row or entity field renders blank.

### Considered and rejected

- **Client-side inspector over `/graph` + `/trace` (no new endpoints):** correct
  by the fences but downloads the full object set and full event payloads to a
  phone for every lookup. Rejected; two small projections instead.
- **Adding a `light=1` mode to `/trace`:** modifying an existing endpoint is
  outside the "ADD endpoints" exception. New `/lab/log` instead.
- **A graph visualization pane:** already on INTERFACE.md's deferred list; stays
  deferred.
- **Auto-opening the orientation strip on first visit (localStorage flag):**
  client state for cosmetics; a visible collapsed summary is enough.

### Deferred (recorded, not built)

- Full-text search over the log (server-side would want an index — fence: no
  side caches; client-side over `/lab/log` pages is possible but heavy).
- Fork diff view; LLM-narrated entries (INTERFACE.md deferred list, unchanged).
- Seam version diff (old body vs proposed) on the entity page of a seam artifact.
- Per-branch LLM cost rollup (sum of `llm.responded.cost_usd` by behavior run —
  projectable, but a new aggregate endpoint; propose separately if wanted).

No fence changes are needed; nothing behind the gate is touched.
