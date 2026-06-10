# lab pack

The activegraph-lab research agent as one layered pack on the activegraph-packs core pack (ADR-003). One mission, branches of inquiry, gated decisions; everything else is a core primitive.

## Dependencies

- `requires = ["core"]`
- `integrates_with = ["communication", "research", "codebase", "tool_gateway", "memory_gateway"]` — all optional, degrade gracefully:
  - no tool_gateway → ingest cannot crawl (the friction is itself recordable evidence)
  - no communication → no threads, no answer behavior firing
  - no research/codebase → dispatched tasks surface capability-gap observations

## Object types

| Type | Purpose |
|---|---|
| `mission` | The lab's single standing purpose (title, statement, target_url, status) |
| `branch` | A line of inquiry; also a thread (ADR-004) and a fork anchor (status: proposed/scoped/active/interpreting/decided/archived; authority: auto/gated) |
| `decision` | A gate record (kind: promote/publish/schema_change/dependency_pin/other; status: pending/approved/rejected; evidence_refs) |

Evidence is core `observation`/`evaluation`; outputs are core `artifact`; work is core `task`; pages are core `source`.

## Relation types

| Relation | From → To |
|---|---|
| `has_branch` | mission → branch |
| `forked_from` | branch → branch |
| `produced` | branch → artifact |
| `supported_by` | branch → observation/evaluation |
| `dispatched` | branch → task |
| `discusses` | comm_thread → branch (communication loaded only) |

Written per the real `add_relation(source, target, type)` signature — see docs/ARCHITECTURE.md for the convention split in the packs repo.

## Behavior map

```
mission.created (status=active)
  → ingest
      creates capability_call("web.fetch_url") per page   [tool_gateway executes]

source.created (kind=tool_result, lab fetch)              [created by result_sourcer]
  → ingest
      creates observation(metadata.lab=site_claim) + grounds relation
      queues same-domain links (depth ≤ 2, pages ≤ 30)
      patches mission.metadata.crawl  ← one progress event per page

observation.created (lab=site_claim)
  → plan (llm)
      creates branch(proposed, gated) + has_branch + supported_by
      reasoning narrated in the event payload

branch → active (created or patched)
  → work
      creates task(routing tags) + dispatched relation + probe patch

patch.applied (probe, one boundary later)
  → work
      no pack reacted → observation(capability_gap) + task blocked

task → done/rejected
  → work
      creates evaluation(lab=task_outcome)

evaluation.created (lab=task_outcome)
  → interpret (llm)
      creates observation(interpretation) + supported_by
      branch → interpreting; creates decision(promote, pending)
      outcome=follow_up → child branch + forked_from

decision.created (pending)
  → gate
      patches decision.metadata.approval_requested_at  ← the inbox event

decision → approved/rejected (owner, via API/UI/steering)
  → gate
      promote: branch → decided / archived
      publish: artifact → published / rejected
      artifact published without approval → reverted + violation observation

comm_message.created (channel=lab, inbound)
  → answer (llm)
      creates comm_response_candidate with "— as of event N" stamp +
      provenance refs; steering (pause/resume/approve/reject) also writes
      the object mutation
```

## Usage

Bootstrap branch zero (mock LLM, canned pages — no API key):

```python
from activegraph import Graph, Runtime
from packs.core import pack as core_pack, CoreSettings
from packs.tool_gateway import pack as tg_pack, ToolGatewaySettings
from lab_pack import pack as lab_pack, LabSettings
from lab_pack.llm import LabMockProvider
from lab_pack.tools import register_web_fetch, create_mission_fn

register_web_fetch()  # live urllib fetcher; fixtures register canned pages
rt = Runtime(Graph(), llm_provider=LabMockProvider())
rt.load_pack(core_pack, settings=CoreSettings())
rt.load_pack(tg_pack, settings=ToolGatewaySettings())
rt.load_pack(lab_pack, settings=LabSettings())
create_mission_fn(rt.graph, "Grow the evidence base", target_url="https://activegraph.ai")
rt.run_until_idle()
```

Talk to a branch (communication pack loaded):

```python
from lab_pack.tools import send_branch_message_fn
thread_id, msg = send_branch_message_fn(rt.graph, branch_id, "what's the state here?")
rt.run_until_idle()
# reply lands as a comm_response_candidate with an event-horizon stamp
```

## Notes

- Fixtures: `python lab_pack/fixtures/run_fixtures.py` — deterministic, no API key.
- When the chat pack is also loaded, its generic responder may produce a second candidate for the same message; the lab's candidate is created later in the cascade and wins the turn.
- In-process registries are caches; on `Runtime.load` resume they must be rebuilt (replay does not re-fire behaviors) — the server does this.
