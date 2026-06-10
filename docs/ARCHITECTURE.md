# ARCHITECTURE

The lab is one layered pack (`lab_pack/`) on the activegraph-packs core pack, composed with upstream infrastructure packs in a bundle. CONTRACT.md governs. ADR-003/004/005/006 set the shape.

## The pack

`lab_pack/` follows the packs repo's `_template` layout: `__init__.py`, `object_types.py`, `behaviors.py`, `tools.py`, `settings.py`, `prompts/`, `fixtures/`, README, CHANGELOG. `requires = ["core"]`. `integrates_with = ["communication", "research", "codebase", "tool_gateway", "memory_gateway"]` — all optional, degrade gracefully. Consumed packs come from the pinned activegraph-packs git dependency; the lab pack is registered under the `activegraph.packs` entry point in this repo's pyproject.toml.

## Object types and relations

Three lab types: `mission` (title, statement, target_url, status), `branch` (title, intent, status, parent_branch_id, fork_event_id, authority), `decision` (subject_ref, kind, status, rationale, evidence_refs). All other nouns are core types. Relations: mission `has_branch` branch, branch `forked_from` branch, branch `produced` artifact, branch `supported_by` observation/evaluation, branch `dispatched` task, thread `discusses` branch (only when communication is loaded).

Relation call convention: this repo writes relations per the actual `Graph.add_relation(source_id, target_id, type)` signature (as the chat pack does), because runtime view traversal and relation queries match on the real source/target fields. The packs repo is split: core/research/tool_gateway write type-first, which the Inspector decodes by assuming inversion. A composed graph therefore contains both encodings; the lab's feed serializer discriminates per relation (object ids contain `#`, relation type names never do). Recorded as upstream friction per ADR-005.

## Behaviors

Seven small reactive behaviors, no orchestrator:

- `ingest` — on mission.created or source-request events: fetch target_url and same-domain links through tool_gateway (registering a `fetch_url` capability if absent), create core sources, then observations for extracted claims. Depth ≤ 2, page cap ≤ 30, a progress event per page.
- `plan` — llm_behavior. On new observations under a mission: identify weakly evidenced claims, create proposed branch objects. Reasoning is narrated in the event payload, never scored by formula.
- `work` — on branch.status → active: create core tasks with routing tags. If no pack reacts within a bounded window, record the capability gap as an observation — a gap is evidence, not an error.
- `interpret` — llm_behavior. On task completion/failure under a lab branch: write a summary observation, link evidence, set the branch to decided or propose follow-ups.
- `gate` — on decision.created (pending): emit an approval-request event. Nothing publishes or self-modifies without an approved decision. No exceptions, including fixtures. When an approved promote decision lands on a branch with ≥2 evidence objects, gate also emits a finding observation — draft_writer's trigger.
- `draft_writer` — llm_behavior. On finding-tagged observations: write a core artifact (kind=blog_draft, markdown with evidence footnotes, claims-coverage review note, provenance block), mirror it to drafts/<slug>.md (graph copy canonical), and open a pending publish decision. OPEN: spec asked rejected drafts to become status `archived`, but the core artifact enum has no such value and core is not ours to change (ADR-005) — rejected drafts map to `rejected` plus a REJECTED header on the mirror file.
- `answer` — llm_behavior, active only when communication is loaded. On message intents in threads that discuss a branch: answer from current graph state, stamp the event horizon, include provenance refs. Steering messages also write the corresponding object mutation.

## Event taxonomy

The runtime owns the taxonomy: `object.created` / `object.patched` / relation events, plus `llm.requested`/`llm.responded` for llm_behaviors. The lab adds no event types; lab semantics (progress, approval-request, narration) are payloads on graph mutations — progress is an observation patch, an approval request is a pending decision object. The feed is a projection over these events joined with their objects.

## Worker coordination (ADR-006)

No adapters. `work` writes core tasks with routing tags (OPEN: exact tag convention — currently `task.metadata.routing` + tags in metadata). Verified against the packs repo at the current pin: no research or codebase behavior reacts to core task objects (research reacts to `source(kind=research_paper)`), so until upstream adds task-reactive behaviors, dispatch produces capability-gap observations — which is the honest state of the evidence base.

## Two-plane latency model

Two planes over one log. Fast plane: `answer` responds in one behavior cascade by reading current graph state, never waiting on tasks. Slow plane: `ingest`/`work`/`interpret` commit events at their own pace. The fast plane sees the slow plane only through committed events — hence the event-horizon stamp on every answer.

## Bundle and server

`lab_pack/bundle.py: build_lab()` composes core, tool_gateway, secrets, memory_gateway, agent_profile, identity_auth, communication, chat, research, codebase, and lab_pack, then creates the mission for https://activegraph.ai with the `read_the_website` seed branch. `server/` is a thin HTTP server copying the demo_server.py pattern: `/graph`, `/trace`, `/chat`, `/reset`, plus read-only `GET /lab/feed`. SQLite persistence under `data/`, paths overridable via env vars. No new storage, no new state.

## Seams (ADR-012, Phase 4)

Prompts, feed narration templates, and whitelisted settings are the lab's first self-modification surface. A seam is a core artifact (kind=seam, metadata: seam_name, version, parent_version) promoted through a decision (kind=self_modify) that the gate treats exactly like publish. `lab_pack/seams.py` (kernel) resolves each seam_name to the highest approved version with file fallback, hot-loads on approval (no restart), and refuses bodies referencing the kernel manifest at proposal AND load time. Inside behaviors the graph is restricted, so resolution is cache-only — populated exclusively by hot_load and boot-time apply_approved, which is also why a seam can never activate without passing the gate. Replay fidelity: behaviors stamp consumed seam versions onto their outputs (replay never re-fires behaviors, so outputs replay verbatim), and feed templates resolve as-of each entry's event via the approval events in the log.
