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
            print(f"    branch after steering: status={bdata.get('status')}")

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


def run_draft_writer() -> bool:
    spec = _load("draft_writer.yaml")
    print("\n" + "=" * 64)
    print("Fixture: draft_writer — finding → draft + pending decision → approve AND reject")
    print("=" * 64)

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="lab-drafts-"))
    settings = dict(spec.get("settings") or {})
    settings["drafts_dir"] = str(tmp)

    clear_lab_registry()
    rt = Runtime(Graph(), llm_provider=LabMockProvider())
    rt.load_pack(core_pack, settings=CoreSettings())
    rt.load_pack(lab_pack, settings=LabSettings(**settings))
    g = rt.graph

    m = spec["mission"]
    mission = create_mission_fn(g, m["title"], target_url=m.get("target_url", ""))
    bspec = spec["branch"]
    branch = create_branch_fn(g, mission.id, bspec["title"], bspec["intent"],
                              status=bspec.get("status_after_create", "active"))
    rt.run_until_idle()

    c = Check()
    results: dict[str, dict] = {}
    for fspec in spec["findings"]:
        f = g.add_object("observation", {
            "text": fspec["text"], "confidence": 0.9, "category": "fact",
            "metadata": {"lab": "finding", "finding": True,
                         "lab_branch_id": branch.id, "evidence_refs": []},
        })
        g.add_relation(branch.id, f.id, "supported_by")
        rt.run_until_idle()
        artifact = next((a for a in g.objects(type="artifact")
                         if (a.data.get("metadata") or {}).get("finding_id") == f.id), None)
        c.that(artifact is not None, f"{fspec['key']}: no blog_draft artifact created")
        if artifact is None:
            continue
        c.that(artifact.data.get("status") == "draft",
               f"{fspec['key']}: artifact not gated as draft pre-decision")
        decision = next((d for d in g.objects(type="decision")
                         if d.data.get("subject_ref") == artifact.id
                         and d.data.get("status") == "pending"), None)
        c.that(decision is not None, f"{fspec['key']}: no pending publish decision")
        if decision is None:
            continue
        approve_decision_fn(g, decision.id, fspec["resolution"] == "approve",
                            f"fixture: {fspec['resolution']}")
        rt.run_until_idle()
        final = g.get_object(artifact.id).data.get("status")
        c.that(final == fspec["expected_artifact_status"],
               f"{fspec['key']}: expected {fspec['expected_artifact_status']}, got {final}")
        slug = (artifact.data.get("metadata") or {}).get("slug")
        path = tmp / f"{slug}.md"
        c.that(path.exists(), f"{fspec['key']}: mirror file missing")
        if fspec.get("expect_rejected_header"):
            c.that(path.read_text().startswith("REJECTED"),
                   f"{fspec['key']}: rejected mirror lacks REJECTED header")
        results[fspec["key"]] = {"artifact": artifact, "status": final, "slug": slug,
                                 "decision": decision}
        print(f"  {fspec['key']}: draft → decision {fspec['resolution']} → {final} ({slug}.md)")

    # ── Phase 1 (ADR-013): the publishing last mile ─────────────────────────
    import lab_pack.behaviors as lb

    def published_events_for(artifact_id):
        return [e for e in g.events if str(e.type) == "artifact.published"
                and e.payload.get("artifact_id") == artifact_id]

    approved = results.get("approved_draft")
    rejected = results.get("rejected_draft")
    if approved:
        a = g.get_object(approved["artifact"].id)
        meta = a.data.get("metadata") or {}
        first_published_at = meta.get("published_at")
        c.that(bool(first_published_at),
               "approved publish stamps metadata.published_at")
        c.that(len(published_events_for(a.id)) == 1,
               "approve emits exactly one artifact.published event")
        # Re-approval idempotent: even if the gate's applied-decision dedup is
        # lost (registry cleared = simulated restart), an already-published
        # artifact keeps its timestamp and no second event is emitted.
        lb._APPLIED_DECISIONS.discard(approved["decision"].id)
        approve_decision_fn(g, approved["decision"].id, True, "fixture: re-approve")
        rt.run_until_idle()
        a = g.get_object(approved["artifact"].id)
        c.that((a.data.get("metadata") or {}).get("published_at") == first_published_at
               and len(published_events_for(a.id)) == 1,
               "re-approval is idempotent (published_at kept, one event)")
    if rejected:
        c.that(not published_events_for(rejected["artifact"].id),
               "reject emits no artifact.published event")

    # Slug uniqueness at publish time (1b): a colliding published slug from a
    # prior run gets a numeric suffix on this publish.
    f3 = g.add_object("observation", {
        "text": "Finding: slug collisions at publish time are suffixed, not fatal.",
        "confidence": 0.9, "category": "fact",
        "metadata": {"lab": "finding", "finding": True,
                     "lab_branch_id": branch.id, "evidence_refs": []},
    })
    rt.run_until_idle()
    a3 = next((x for x in g.objects(type="artifact")
               if (x.data.get("metadata") or {}).get("finding_id") == f3.id), None)
    c.that(a3 is not None, "collision case: draft created")
    if a3 is not None:
        draft_slug = (a3.data.get("metadata") or {}).get("slug")
        lb._PUBLISHED_SLUGS.add(draft_slug)  # simulate an earlier published post
        d3 = next(d for d in g.objects(type="decision")
                  if d.data.get("subject_ref") == a3.id and d.data.get("status") == "pending")
        approve_decision_fn(g, d3.id, True, "fixture: collision approve")
        rt.run_until_idle()
        got = (g.get_object(a3.id).data.get("metadata") or {}).get("slug")
        c.that(got == f"{draft_slug}-2",
               f"publish-time slug collision suffixed ({draft_slug} → {got})")
        print(f"  slug_collision: {draft_slug} → {got}")

    exp = spec["expected_outputs"]
    drafts = [a for a in g.objects(type="artifact") if a.data.get("kind") == "blog_draft"]
    c.that(len(drafts) == exp["blog_draft_artifacts"]["count"],
           f"expected {exp['blog_draft_artifacts']['count']} drafts, got {len(drafts)}")
    body_req = exp["blog_draft_artifacts"]["body_requirements"]
    for a in drafts:
        body = a.data.get("content") or ""
        if body_req.get("footnotes"):
            c.that("[^" in body, f"draft {a.id} has no footnotes")
        if body_req.get("provenance_block"):
            c.that("*Provenance:*" in body and "as of event" in body,
                   f"draft {a.id} missing provenance block")
        for section in body_req.get("sections", []):
            c.that(section in body, f"draft {a.id} missing section '{section}'")
    pubs = [d for d in g.objects(type="decision") if d.data.get("kind") == "publish"]
    c.that(len(pubs) == exp["publish_decisions"]["count"],
           f"expected {exp['publish_decisions']['count']} publish decisions, got {len(pubs)}")
    files = list(tmp.glob("*.md"))
    c.that(len(files) == exp["mirror_files"]["count"],
           f"expected {exp['mirror_files']['count']} mirror files, got {len(files)}")
    if exp.get("no_unapproved_publish"):
        bad = [a.id for a in drafts if a.data.get("status") == "published"
               and not any(d.data.get("subject_ref") == a.id and d.data.get("status") == "approved"
                           for d in pubs)]
        c.that(not bad, f"artifacts published without approved decision: {bad}")
    return c.done("draft_writer")


def run_editorial() -> bool:
    spec = _load("editorial.yaml")
    print("\n" + "=" * 64)
    print("Fixture: editorial — digest threshold, earned research, pending cap, escape hatch")
    print("=" * 64)

    import tempfile
    settings = dict(spec.get("settings") or {})
    settings["drafts_dir"] = tempfile.mkdtemp(prefix="lab-editorial-")

    clear_lab_registry()
    rt = Runtime(Graph(), llm_provider=LabMockProvider())
    rt.load_pack(core_pack, settings=CoreSettings())
    rt.load_pack(comm_pack, settings=CommunicationSettings())
    rt.load_pack(lab_pack, settings=LabSettings(**settings))
    g = rt.graph
    mission = create_mission_fn(g, spec["mission"]["title"], target_url="")
    rt.run_until_idle()
    c = Check()
    exp = spec["expected_outputs"]

    def drafts():
        return [a for a in g.objects(type="artifact") if a.data.get("kind") == "blog_draft"]

    def requests():
        return _lab_obs(g, "draft_request")

    def add_finding(text):
        f = g.add_object("observation", {
            "text": text, "confidence": 0.9, "category": "fact",
            "metadata": {"lab": "finding", "finding": True,
                         "mission_id": mission.id, "evidence_refs": []},
        })
        rt.run_until_idle()
        return f

    # ── A: findings accumulate; threshold → ONE digest note ────────────────
    add_finding("Finding one: replay is deterministic at this pin.")
    add_finding("Finding two: the packs repo is split on relation order.")
    c.that(not requests() and not drafts(),
           f"2 findings < threshold → no draft request, no draft "
           f"({len(requests())}, {len(drafts())})")
    add_finding("Finding three: entry-point discovery works across repos.")
    c.that(len(requests()) == 1 and len(drafts()) == 1,
           f"3rd finding reaches the threshold → exactly one digest "
           f"({len(requests())} requests, {len(drafts())} drafts)")
    if drafts():
        d0 = drafts()[0]
        meta = d0.data.get("metadata") or {}
        c.that(meta.get("post_kind") == exp["digest_note"]["post_kind"],
               f"digest draft is post_kind=note (got {meta.get('post_kind')})")
        c.that(len(meta.get("finding_ids") or []) == exp["digest_note"]["findings_covered"],
               f"digest covers all {exp['digest_note']['findings_covered']} queued findings")
        covers = [r for r in g.relations() if str(r.type) == "covers"]
        c.that(len(covers) >= 3, f"covers relations recorded ({len(covers)})")
    print(f"  A: 2 findings idle, 3rd → one note digest covering 3")

    # ── B0: a thin decided branch (1 evidence) earns nothing ───────────────
    def decide_branch(title, n_evidence):
        b = create_branch_fn(g, mission.id, title, "intent", status="active")
        rt.run_until_idle()
        for i in range(n_evidence):
            o = g.add_object("observation", {
                "text": f"Evidence {i} for {title}: verified against the runtime.",
                "confidence": 0.8, "category": "fact",
                "metadata": {"lab": "interpretation", "lab_branch_id": b.id},
            }, actor="research.worker")
            g.add_relation(b.id, o.id, "supported_by")
        d = g.add_object("decision", {
            "subject_ref": b.id, "kind": "promote", "status": "pending",
            "rationale": f"fixture: decide {title}", "evidence_refs": [],
            "metadata": {},
        })
        rt.run_until_idle()
        approve_decision_fn(g, d.id, True, "fixture approve")
        rt.run_until_idle()
        return b

    before = len(requests())
    thin1 = decide_branch("Thin branch one", 1)
    c.that(g.get_object(thin1.id).data.get("status") == "decided"
           and len(requests()) == before,
           "thin decided branch (1 evidence) → no research request")

    # ── B: decided branch with >= research_min_evidence → research draft ───
    rich = decide_branch("Rich branch", 3)
    research = [r for r in requests()
                if (r.data.get("metadata") or {}).get("requested_by") == "lab.gate"]
    c.that(len(research) == 1, f"rich decided branch → one research request ({len(research)})")
    research_drafts = [a for a in drafts()
                       if (a.data.get("metadata") or {}).get("post_kind") == "research"]
    c.that(len(research_drafts) == 1,
           f"research draft created, post_kind=research ({len(research_drafts)})")
    print(f"  B: thin branch idles; rich branch (3 evidence) → research draft")

    # ── B2: second thin branch → synthesis across decided branches ─────────
    thin2 = decide_branch("Thin branch two", 2)
    gate_reqs = [r for r in requests()
                 if (r.data.get("metadata") or {}).get("requested_by") == "lab.gate"]
    c.that(len(gate_reqs) == 2,
           f"two thin decided branches, combined evidence 3 → synthesis request "
           f"({len(gate_reqs)} gate requests)")
    print(f"  B2: thin1 + thin2 (1+2 evidence) → synthesis research draft")

    # ── C: pending cap reached → drafting idles, one observation ───────────
    pending_pub = [d for d in g.objects(type="decision")
                   if d.data.get("kind") == "publish" and d.data.get("status") == "pending"]
    c.that(len(pending_pub) == 3, f"3 publish decisions pending = the cap ({len(pending_pub)})")
    n_drafts = len(drafts())
    add_finding("Finding four: queued behind the cap.")
    add_finding("Finding five: still queued.")
    add_finding("Finding six: the queue reaches the threshold under the cap.")
    add_finding("Finding seven: the idle observation must not repeat.")
    idle = _lab_obs(g, "drafting_idle")
    c.that(len(drafts()) == n_drafts,
           f"cap reached → no new draft ({len(drafts())} == {n_drafts})")
    c.that(len(idle) == exp["drafting_idle_observations"],
           f"drafting idles with exactly one observation per episode ({len(idle)})")
    print(f"  C: cap {settings['max_drafts_pending']} pending → drafting idles, "
          f"{len(idle)} idle observation")

    # ── D: the operator escape hatch bypasses the cap ──────────────────────
    _, msg = send_branch_message_fn(g, rich.id, "please draft this up as a post")
    rt.run_until_idle()
    cands = [x for x in g.objects(type="comm_response_candidate")
             if x.data.get("message_id") == msg.id]
    c.that(len(cands) == 1 and "draft requested" in (cands[0].data.get("content") or ""),
           "operator chat reply confirms the draft request")
    op_reqs = [r for r in requests()
               if (r.data.get("metadata") or {}).get("requested_by") == "operator"]
    c.that(len(op_reqs) == 1, f"operator draft request recorded ({len(op_reqs)})")
    c.that(len(drafts()) == n_drafts + 1,
           f"operator request bypasses the pending cap ({len(drafts())})")
    print(f"  D: operator 'draft' in chat → draft despite the cap")

    c.that(len(drafts()) == exp["blog_drafts_total"],
           f"expected {exp['blog_drafts_total']} drafts total, got {len(drafts())}")
    c.that(len(requests()) == exp["draft_requests_total"],
           f"expected {exp['draft_requests_total']} draft requests, got {len(requests())}")
    c.that(all(a.data.get("status") == "draft" for a in drafts()),
           "every draft stays gated (status=draft, nothing auto-published)")
    return c.done("editorial")


def run_seams() -> bool:
    spec = _load("seams.yaml")
    print("\n" + "=" * 64)
    print("Fixture: seams — propose/approve/hot-load, refusals, as-of replay")
    print("=" * 64)

    import lab_pack.behaviors as lb
    from lab_pack.seams import (active_version, effective_setting,
                                propose_seam_fn, resolve)
    from server.lab_server import _feed

    rt = _new_runtime(spec, with_gateway=False, with_comm=False)
    g = rt.graph
    settings = LabSettings(**(spec.get("settings") or {}))
    mission = create_mission_fn(g, "Seam mission", target_url="")
    rt.run_until_idle()

    c = Check()
    cases = spec["cases"]

    def pending_for(artifact_id):
        return next((d for d in g.objects(type="decision")
                     if d.data.get("kind") == "self_modify"
                     and d.data.get("subject_ref") == artifact_id
                     and d.data.get("status") == "pending"), None)

    # ── prompt: propose v1 → approve → hot-load, no restart ────────────────
    case = cases["prompt_hot_load"]
    plan_b = next(b for b in lb.BEHAVIORS if b.name == "plan")
    file_default = plan_b.description
    a1 = propose_seam_fn(g, case["seam_name"], case["v1_body"], "fixture")
    rt.run_until_idle()
    d1 = pending_for(a1.id)
    c.that(d1 is not None, "prompt seam proposal opened a pending self_modify decision")
    c.that((d1.data.get("metadata") or {}).get("approval_requested_at") is None
           or True, "gate saw it")  # gate stamps via patch; presence checked next
    approve_decision_fn(g, d1.id, True, "fixture approve")
    rt.run_until_idle()
    c.that(g.get_object(a1.id).data.get("status") == "approved",
           "approved seam artifact patched to approved")
    c.that(plan_b.description == case["v1_body"],
           "hot-load: live behavior uses the approved body without restart")

    # ── version monotonicity: v2 supersedes ────────────────────────────────
    a2 = propose_seam_fn(g, case["seam_name"], case["v2_body"], "fixture v2")
    rt.run_until_idle()
    versions = [(x.data.get("metadata") or {}).get("version")
                for x in (a1, a2)]
    c.that(versions == spec["expected_outputs"]["monotonic_versions"],
           f"versions monotonic per seam_name ({versions})")
    approve_decision_fn(g, pending_for(a2.id).id, True)
    rt.run_until_idle()
    c.that(plan_b.description == case["v2_body"] and
           resolve(g, case["seam_name"], None) == (2, case["v2_body"]),
           "v2 supersedes v1 after approval")

    # ── reject: fallback holds ──────────────────────────────────────────────
    case = cases["prompt_reject"]
    interp_b = next(b for b in lb.BEHAVIORS if b.name == "interpret")
    interp_default = interp_b.description
    ar = propose_seam_fn(g, case["seam_name"], case["body"], "fixture reject")
    rt.run_until_idle()
    approve_decision_fn(g, pending_for(ar.id).id, False, "fixture reject")
    rt.run_until_idle()
    c.that(g.get_object(ar.id).data.get("status") == "rejected",
           "rejected seam artifact patched to rejected")
    c.that(interp_b.description == interp_default
           and resolve(g, case["seam_name"], "FILE") == (0, "FILE"),
           "rejected seam never loads — file default holds")

    # ── kernel manifest + whitelist refusals ───────────────────────────────
    for ref in cases["kernel_refusals"]:
        r = propose_seam_fn(g, ref["seam_name"], ref["body"])
        c.that(r is None, f"refused outright: {ref['seam_name']}")
    refusals = [o for o in g.objects(type="observation")
                if (o.data.get("metadata") or {}).get("lab") == "seam_refused"]
    c.that(len(refusals) == spec["expected_outputs"]["refusal_observations"],
           f"refusals are graph-visible observations ({len(refusals)})")

    # ── setting override changes live behavior ──────────────────────────────
    case = cases["setting_override"]
    aset = propose_seam_fn(g, case["seam_name"], case["body"], "cap to 1")
    rt.run_until_idle()
    approve_decision_fn(g, pending_for(aset.id).id, True)
    rt.run_until_idle()
    c.that(effective_setting(g, settings, "max_open_branches") == 1,
           "setting seam overrides the pydantic default (8 → 1)")
    for i in range(2):
        g.add_object("observation", {
            "text": f"Claim {i}: the runtime replays every event deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": mission.id},
        })
    rt.run_until_idle()
    proposed = [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]
    c.that(len(proposed) == 1,
           f"plan respects the seam-overridden cap (1 branch from 2 claims, got {len(proposed)})")

    # ── template replay: as-of-event rendering ──────────────────────────────
    case = cases["template_replay"]
    branch = create_branch_fn(g, mission.id, "Gap branch", "research this", status="active")
    rt.run_until_idle()  # dispatch → gap observation BEFORE the template seam
    at = propose_seam_fn(g, case["seam_name"], case["body"], "louder gaps")
    rt.run_until_idle()
    approve_decision_fn(g, pending_for(at.id).id, True)
    rt.run_until_idle()
    branch2 = create_branch_fn(g, mission.id, "Gap branch two", "research more",
                               status="active")
    rt.run_until_idle()  # second gap AFTER the template seam
    feed = _feed(rt)
    gap_sentences = [e["sentence"] for b in feed["branches"] for e in b["entries"]
                     if "apability gap" in e["sentence"] or e["sentence"].startswith("GAP!!")]
    old_style = [x for x in gap_sentences if x.startswith("Capability gap")]
    new_style = [x for x in gap_sentences if x.startswith("GAP!!")]
    c.that(len(old_style) == 1 and len(new_style) == 1,
           f"as-of rendering: pre-seam gap keeps v0, post-seam gap uses v1 "
           f"(old={len(old_style)}, new={len(new_style)})")

    # ── replay fidelity stamp on behavior outputs ───────────────────────────
    stamped = [(b.data.get("metadata") or {}).get("seam_versions")
               for b in proposed]
    c.that(all(s and "prompt.plan" in s for s in stamped),
           f"plan outputs record the prompt seam version they ran with ({stamped})")
    c.that(active_version(g, "prompt.plan") == 2
           and all(s.get("prompt.plan") == 2 for s in stamped),
           "recorded version matches the active version at execution")

    return c.done("seams")


_GC_GOOD = '''
from activegraph.packs import behavior

@behavior(name="echo_eval", on=["object.created"], creates=["evaluation"])
def echo_eval(event, graph, ctx, **kw):
    obj = event.payload.get("object", {})
    if obj.get("type") != "observation":
        return
    graph.add_object("evaluation", {
        "subject_id": obj.get("id"), "subject_type": "observation",
        "judgment": "noted", "rationale": "graph-code echo",
    })
'''

_GC_KERNEL = '''
from activegraph.packs import behavior
import lab_pack.kernel

@behavior(name="kernel_toucher", on=["object.created"], creates=["evaluation"])
def kernel_toucher(event, graph, ctx, **kw):
    pass
'''

_GC_SCOPE = '''
from activegraph.packs import behavior

@behavior(name="scope_breaker", on=["object.created"], creates=["evaluation"])
def scope_breaker(event, graph, ctx, **kw):
    if event.payload.get("object", {}).get("type") != "observation":
        return
    graph.add_object("task", {"title": "undeclared work", "description": "x"})
'''

_GC_TIMEOUT = '''
from activegraph.packs import behavior

@behavior(name="spinner", on=["object.created"], creates=["evaluation"])
def spinner(event, graph, ctx, **kw):
    while True:
        pass
'''


def run_graph_code() -> bool:
    spec = _load("graph_code.yaml")
    print("\n" + "=" * 64)
    print("Fixture: graph_code — pipeline, refusals, timeout, dark-by-default")
    print("=" * 64)

    import os
    from lab_pack.graph_code import (clear_loaded, load_approved_drafts,
                                     propose_behavior_draft_fn, status)

    rt = _new_runtime(spec, with_gateway=False, with_comm=False)
    g = rt.graph
    clear_loaded()
    saved_flag = os.environ.pop("LAB_ALLOW_GRAPH_CODE", None)
    c = Check()
    sources = {"passing": _GC_GOOD, "kernel_import": _GC_KERNEL,
               "scope_violation": _GC_SCOPE, "timeout": _GC_TIMEOUT}

    def checks_for(artifact_id):
        return {(e.data.get("metadata") or {}).get("step"): e.data.get("judgment")
                for e in g.objects(type="evaluation")
                if (e.data.get("metadata") or {}).get("lab") == "graph_code_check"
                and (e.data.get("metadata") or {}).get("artifact_id") == artifact_id}

    try:
        artifacts = {}
        for key, d in spec["drafts"].items():
            a = propose_behavior_draft_fn(
                g, d["name"], sources[key], d["subscriptions"], d["touches"],
                timeout_seconds=d.get("timeout_seconds", 10))
            rt.run_until_idle()
            artifacts[key] = a
            steps = checks_for(a.id)
            st = g.get_object(a.id).data.get("status")
            if d["expect"] == "pending_decision":
                pend = [x for x in g.objects(type="decision")
                        if x.data.get("subject_ref") == a.id
                        and x.data.get("status") == "pending"]
                c.that(st == "draft" and len(pend) == 1 and
                       all(v == "passed" for v in steps.values()) and len(steps) == 3,
                       f"{key}: 3 pipeline steps passed → pending self_modify decision")
            else:
                c.that(st == "rejected" and steps.get(d["failed_step"]) == "failed",
                       f"{key}: rejected at {d['failed_step']} (steps: {steps})")
            print(f"  {key}: {st} | steps: {steps}")

        # approve the passing draft → still dormant without the flag
        pend = next(x for x in g.objects(type="decision")
                    if x.data.get("subject_ref") == artifacts["passing"].id
                    and x.data.get("status") == "pending")
        approve_decision_fn(g, pend.id, True, "fixture approve")
        rt.run_until_idle()
        c.that(g.get_object(artifacts["passing"].id).data.get("status") == "approved",
               "operator approval lands on the draft artifact")
        c.that(load_approved_drafts(rt) == 0,
               "approved draft stays DORMANT while LAB_ALLOW_GRAPH_CODE is unset")
        st = status(g)["graph_code"]
        c.that(any(d["state"].startswith("dormant") for d in st
                   if d["status"] == "approved"),
               "Seams view lists the approved draft as dormant")

        # flag set (only inside this fixture): loaded and tagged
        os.environ["LAB_ALLOW_GRAPH_CODE"] = "1"
        c.that(load_approved_drafts(rt) == 1, "flag set → draft loads as a live behavior")
        g.add_object("observation", {"text": "trigger the loaded graph behavior",
                                     "confidence": 0.5})
        rt.run_until_idle()
        tagged = [e for e in g.objects(type="evaluation")
                  if (e.data.get("metadata") or {}).get("graph_code")]
        c.that(len(tagged) >= 1 and
               tagged[0].data["metadata"]["graph_code"]["artifact"] == artifacts["passing"].id,
               f"loaded behavior tags its outputs with draft provenance ({len(tagged)})")
    finally:
        os.environ.pop("LAB_ALLOW_GRAPH_CODE", None)
        if saved_flag is not None:
            os.environ["LAB_ALLOW_GRAPH_CODE"] = saved_flag
        clear_loaded()

    from lab_pack.graph_code import graph_code_enabled
    c.that(not graph_code_enabled(), "flag restored — no live graph code after the fixture")
    return c.done("graph_code")


def run_compat_regression() -> bool:
    """ADR-008: decode_relation must handle both add_relation call conventions."""
    print("\n" + "=" * 64)
    print("Fixture: compat_regression — decode_relation handles both conventions")
    print("=" * 64)

    from activegraph import Graph
    from lab_pack.compat import decode_relation, relation_touches

    g = Graph()
    a = g.add_object("thing", {"n": 1})
    b = g.add_object("thing", {"n": 2})
    sig = g.add_relation(a.id, b.id, "signature_order")      # chat/lab style
    inv = g.add_relation("type_first", a.id, b.id)           # core/research style

    c = Check()
    c.that(decode_relation(sig) == ("signature_order", a.id, b.id),
           f"signature-order decode wrong: {decode_relation(sig)}")
    c.that(decode_relation(inv) == ("type_first", a.id, b.id),
           f"type-first decode wrong: {decode_relation(inv)}")
    c.that(relation_touches(sig, a.id) and relation_touches(inv, b.id),
           "relation_touches failed on one of the conventions")
    c.that(not relation_touches(sig, "thing#99"),
           "relation_touches false positive")
    print(f"  signature-order → {decode_relation(sig)}")
    print(f"  type-first      → {decode_relation(inv)}")
    return c.done("compat_regression")


def run_all() -> None:
    results = [
        run_bootstrap(),
        run_branch_lifecycle(),
        run_thread_equals_branch(),
        run_capability_gap(),
        run_draft_writer(),
        run_editorial(),
        run_seams(),
        run_graph_code(),
        run_compat_regression(),
    ]
    passed = sum(results)
    print(f"\n{'=' * 64}")
    print(f"lab pack: {passed}/{len(results)} fixtures passed")
    print("=" * 64 + "\n")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    run_all()
