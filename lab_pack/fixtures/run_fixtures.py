"""Lab pack fixture runner — deterministic, no LLM key, no network.

Each fixture is a YAML scenario in this directory; this runner drives the
runtime accordingly and checks expected_outputs. Exit code 0 on success,
1 on failure (packs-repo convention).

Run:
    python lab_pack/fixtures/run_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parents[2]))

from activegraph import Graph, Runtime
from packs.core import pack as core_pack, CoreSettings
from packs.tool_gateway import pack as tg_pack, ToolGatewaySettings
from packs.communication import pack as comm_pack, CommunicationSettings

from lab_pack import pack as lab_pack, LabSettings
from lab_pack.behaviors import clear_lab_registry
from lab_pack.llm import LabMockProvider
from lab_pack.tools import (
    activate_branch_fn,
    approve_decision_fn,
    complete_task_fn,
    create_branch_fn,
    create_mission_fn,
    register_web_fetch,
    send_branch_message_fn,
)

_DIR = Path(__file__).parent


def _load(name: str) -> dict:
    with open(_DIR / name) as f:
        return yaml.safe_load(f)


def _new_runtime(spec: dict, *, with_gateway: bool, with_comm: bool) -> Runtime:
    clear_lab_registry()
    rt = Runtime(Graph(), llm_provider=LabMockProvider())
    rt.load_pack(core_pack, settings=CoreSettings())
    if with_gateway:
        rt.load_pack(tg_pack, settings=ToolGatewaySettings())
    if with_comm:
        rt.load_pack(comm_pack, settings=CommunicationSettings())
    rt.load_pack(lab_pack, settings=LabSettings(**(spec.get("settings") or {})))
    return rt


def _lab_obs(graph, kind: str) -> list:
    return [o for o in graph.objects(type="observation")
            if (o.data.get("metadata") or {}).get("lab") == kind]


class Check:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def that(self, cond: bool, msg: str) -> None:
        if not cond:
            self.failures.append(msg)

    def done(self, name: str) -> bool:
        if self.failures:
            print("  FAIL:")
            for f in self.failures:
                print(f"    {f}")
            return False
        print("  PASS")
        return True


def run_bootstrap() -> bool:
    spec = _load("bootstrap.yaml")
    print("\n" + "=" * 64)
    print("Fixture: bootstrap — mission → crawl → claims → proposed branches")
    print("=" * 64)

    pages = spec["pages"]

    def canned_fetch(url: str, **_kw) -> dict:
        clean = url.rstrip("/")
        for key, html in pages.items():
            if key.rstrip("/") == clean:
                return {"url": url, "status": 200, "content": html}
        return {"url": url, "status": 404, "content": ""}

    rt = _new_runtime(spec, with_gateway=True, with_comm=False)
    register_web_fetch(canned_fetch, overwrite=True)

    m = spec["mission"]
    mission = create_mission_fn(rt.graph, m["title"], m.get("statement", ""), m["target_url"])
    rt.run_until_idle()

    g = rt.graph
    sources = [s for s in g.objects(type="source") if s.data.get("kind") == "tool_result"]
    claims = _lab_obs(g, "site_claim")
    branches = [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]
    crawl = (g.get_object(mission.id).data.get("metadata") or {}).get("crawl") or {}

    print(f"  pages fetched: {crawl.get('fetched')}  sources: {len(sources)}  "
          f"site_claims: {len(claims)}  proposed branches: {len(branches)}")
    for b in branches[:4]:
        print(f"    [{b.id}] {b.data['title'][:64]}")
        print(f"        reasoning: {(b.data['metadata'].get('reasoning') or '')[:88]}")

    exp = spec["expected_outputs"]
    c = Check()
    c.that(len(sources) >= exp["sources"]["min_count"],
           f"expected >= {exp['sources']['min_count']} tool_result sources, got {len(sources)}")
    c.that(len(claims) >= exp["site_claim_observations"]["min_count"],
           f"expected >= {exp['site_claim_observations']['min_count']} site_claim observations, got {len(claims)}")
    c.that(len(branches) >= exp["proposed_branches"]["min_count"],
           f"expected >= {exp['proposed_branches']['min_count']} proposed branches, got {len(branches)}")
    if exp["proposed_branches"].get("reasoning_required"):
        c.that(all((b.data["metadata"].get("reasoning") or "").strip() for b in branches),
               "a proposed branch is missing narrated reasoning")
    if exp.get("crawl_progress_on_mission"):
        c.that(bool(crawl.get("fetched")), "mission.metadata.crawl progress missing")
    return c.done("bootstrap")


def run_branch_lifecycle() -> bool:
    spec = _load("branch_lifecycle.yaml")
    print("\n" + "=" * 64)
    print("Fixture: branch_lifecycle — dispatch → complete → interpret → gate (approve AND reject)")
    print("=" * 64)

    rt = _new_runtime(spec, with_gateway=False, with_comm=False)
    g = rt.graph
    m = spec["mission"]
    mission = create_mission_fn(g, m["title"], target_url=m.get("target_url", ""))
    rt.run_until_idle()

    results: dict[str, str] = {}
    for bspec in spec["branches"]:
        branch = create_branch_fn(g, mission.id, bspec["title"], bspec["intent"])
        rt.run_until_idle()
        activate_branch_fn(g, branch.id)
        rt.run_until_idle()

        tasks = [t for t in g.objects(type="task")
                 if (t.data.get("metadata") or {}).get("lab_branch_id") == branch.id]
        if not tasks:
            results[bspec["key"]] = "NO_TASK"
            continue
        comp = bspec["completion"]
        complete_task_fn(g, tasks[0].id, comp["result_summary"], comp.get("success", True))
        rt.run_until_idle()

        pending = [d for d in g.objects(type="decision")
                   if d.data.get("subject_ref") == branch.id and d.data.get("status") == "pending"]
        if not pending:
            results[bspec["key"]] = "NO_DECISION"
            continue
        approve_decision_fn(g, pending[0].id, bspec["resolution"] == "approve",
                            f"fixture: {bspec['resolution']}")
        rt.run_until_idle()
        final = g.get_object(branch.id).data.get("status")
        results[bspec["key"]] = final
        print(f"  {bspec['key']}: task → done → decision {bspec['resolution']} → branch {final}")

    exp = spec["expected_outputs"]
    decisions = g.objects(type="decision")
    evals = [e for e in g.objects(type="evaluation")
             if (e.data.get("metadata") or {}).get("lab") == "task_outcome"]
    interp = _lab_obs(g, "interpretation")

    c = Check()
    c.that(len(g.objects(type="task")) >= exp["tasks"]["min_count"],
           f"expected >= {exp['tasks']['min_count']} tasks")
    c.that(len(evals) >= exp["task_outcome_evaluations"]["min_count"],
           f"expected >= {exp['task_outcome_evaluations']['min_count']} task_outcome evaluations, got {len(evals)}")
    c.that(len(interp) >= exp["interpretation_observations"]["min_count"],
           f"expected >= {exp['interpretation_observations']['min_count']} interpretation observations, got {len(interp)}")
    c.that(len(decisions) == exp["decisions"]["count"],
           f"expected {exp['decisions']['count']} decisions, got {len(decisions)}")
    if exp["decisions"].get("approval_requested"):
        c.that(all((d.data.get("metadata") or {}).get("approval_requested_at") for d in decisions),
               "gate did not stamp approval_requested_at on every decision")
    statuses = sorted(d.data.get("status") for d in decisions)
    c.that(statuses == sorted(exp["decisions"]["statuses"]),
           f"expected decision statuses {exp['decisions']['statuses']}, got {statuses}")
    for bspec in spec["branches"]:
        c.that(results.get(bspec["key"]) == bspec["expected_final_status"],
               f"{bspec['key']}: expected final status {bspec['expected_final_status']}, "
               f"got {results.get(bspec['key'])}")
    return c.done("branch_lifecycle")


def run_thread_equals_branch() -> bool:
    spec = _load("thread_equals_branch.yaml")
    print("\n" + "=" * 64)
    print("Fixture: thread_equals_branch — stamped answer + steering mutation")
    print("=" * 64)

    rt = _new_runtime(spec, with_gateway=False, with_comm=True)
    g = rt.graph
    m = spec["mission"]
    mission = create_mission_fn(g, m["title"], target_url=m.get("target_url", ""))
    bspec = spec["branch"]
    branch = create_branch_fn(g, mission.id, bspec["title"], bspec["intent"],
                              status=bspec.get("status_after_create", "active"))
    rt.run_until_idle()

    c = Check()
    thread_id = None
    for mspec in spec["messages"]:
        thread_id, msg = send_branch_message_fn(g, branch.id, mspec["content"], thread_id=thread_id)
        rt.run_until_idle()
        cands = [x for x in g.objects(type="comm_response_candidate")
                 if x.data.get("message_id") == msg.id]
        print(f"  '{mspec['content'][:40]}' → {len(cands)} candidate(s)")
        if mspec.get("expect_reply"):
            c.that(len(cands) == 1, f"expected 1 reply candidate for '{mspec['content'][:30]}'")
        if cands:
            content = cands[0].data.get("content") or ""
            print(f"    reply: {content[:90].replace(chr(10), ' / ')}")
            if mspec.get("expect_stamp"):
                c.that("as of event" in content, "reply missing event-horizon stamp")
        mut = mspec.get("expect_mutation")
        if mut:
            bdata = g.get_object(branch.id).data
            c.that(bdata.get("status") == mut["status"],
                   f"expected branch status {mut['status']}, got {bdata.get('status')}")
            if mut.get("paused_flag"):
                c.that(bool((bdata.get("metadata") or {}).get("paused")),
                       "branch metadata.paused flag not set")
            print(f"    branch after steering: status={bdata.get('status')} "
                  f"paused={bdata.get('metadata', {}).get('paused')}")

    exp = spec["expected_outputs"]
    cands = g.objects(type="comm_response_candidate")
    c.that(len(cands) == exp["response_candidates"]["count"],
           f"expected {exp['response_candidates']['count']} candidates, got {len(cands)}")
    if exp.get("discusses_relation"):
        # Lab relations use the real (source, target, type) signature.
        disc = [r for r in g.relations() if str(r.type) == "discusses"]
        c.that(any(str(r.source) == thread_id and str(r.target) == branch.id for r in disc),
               "discusses(thread → branch) relation missing")
    return c.done("thread_equals_branch")


def run_capability_gap() -> bool:
    spec = _load("capability_gap.yaml")
    print("\n" + "=" * 64)
    print("Fixture: capability_gap — dispatch with no reacting pack → gap evidence")
    print("=" * 64)

    rt = _new_runtime(spec, with_gateway=False, with_comm=False)
    g = rt.graph
    m = spec["mission"]
    mission = create_mission_fn(g, m["title"], target_url=m.get("target_url", ""))
    bspec = spec["branch"]
    branch = create_branch_fn(g, mission.id, bspec["title"], bspec["intent"])
    rt.run_until_idle()
    activate_branch_fn(g, branch.id)
    rt.run_until_idle()

    tasks = g.objects(type="task")
    gaps = _lab_obs(g, "capability_gap")
    print(f"  tasks: {[(t.id, t.data.get('status')) for t in tasks]}")
    for o in gaps:
        print(f"  gap: {o.data['text'][:96]}")

    exp = spec["expected_outputs"]
    c = Check()
    c.that(len(tasks) == exp["tasks"]["count"],
           f"expected {exp['tasks']['count']} task, got {len(tasks)}")
    c.that(bool(tasks) and tasks[0].data.get("status") == exp["tasks"]["final_status"],
           f"expected task status {exp['tasks']['final_status']}")
    c.that(len(gaps) == exp["capability_gap_observations"]["count"],
           f"expected {exp['capability_gap_observations']['count']} gap observation, got {len(gaps)}")
    if exp["capability_gap_observations"].get("linked_to_branch"):
        linked = [r for r in g.relations()
                  if str(r.type) == "supported_by" and str(r.source) == branch.id
                  and gaps and str(r.target) == gaps[0].id]
        c.that(bool(linked), "gap observation not linked supported_by to the branch")
    return c.done("capability_gap")


def run_all() -> None:
    results = [
        run_bootstrap(),
        run_branch_lifecycle(),
        run_thread_equals_branch(),
        run_capability_gap(),
    ]
    passed = sum(results)
    print(f"\n{'=' * 64}")
    print(f"lab pack: {passed}/{len(results)} fixtures passed")
    print("=" * 64 + "\n")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    run_all()
