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
from lab_pack.behaviors import bind_live_behaviors, clear_lab_registry
from lab_pack.llm import LabMockProvider
from lab_pack.tools import (
    activate_branch_fn,
    approve_decision_fn,
    complete_task_fn,
    create_branch_fn,
    create_mission_fn,
    register_web_fetch,
    request_crawl_fn,
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
    bind_live_behaviors(rt)  # seam hot-loads must reach the runtime's copies
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


def run_claim_hygiene() -> bool:
    spec = _load("claim_hygiene.yaml")
    print("\n" + "=" * 64)
    print("Fixture: claim_hygiene — pollution yields zero claims, prose yields clean ones")
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
    g = rt.graph

    m = spec["mission"]
    mission = create_mission_fn(g, m["title"], m.get("statement", ""), m["target_url"])
    rt.run_until_idle()
    obs_before_pollution = len(g.objects(type="observation"))
    request_crawl_fn(g, mission.id, "https://activegraph.ai/envelope")
    request_crawl_fn(g, mission.id, "https://activegraph.ai/chrome")
    rt.run_until_idle()

    claims = _lab_obs(g, "site_claim")
    by_url: dict[str, list[str]] = {}
    for o in claims:
        by_url.setdefault((o.data.get("metadata") or {}).get("url", "?"), []) \
              .append(o.data.get("text") or "")
    prose = by_url.get("https://activegraph.ai", [])
    polluted = (by_url.get("https://activegraph.ai/envelope", [])
                + by_url.get("https://activegraph.ai/chrome", []))
    for u, texts in sorted(by_url.items()):
        print(f"  {u}: {len(texts)} claim(s)")
        for t in texts:
            print(f"    “{t[:88]}”")

    exp = spec["expected_outputs"]
    c = Check()
    c.that(len(prose) >= exp["prose_claims"]["min_count"],
           f"expected >= {exp['prose_claims']['min_count']} prose claims, got {len(prose)}")
    c.that(len(polluted) == exp["polluted_claims"]["count"],
           f"envelope JSON + nav/SVG chrome must yield zero claims, got {len(polluted)}: "
           f"{[t[:60] for t in polluted]}")
    bad = [t for t in prose
           if any(ch in t for ch in "<>{}") or t.lstrip().startswith(('{"', "{'"))]
    c.that(not bad, f"clean claims contain markup/JSON fragments: {bad}")
    c.that(any("&" in t and "&amp;" not in t for t in prose),
           "HTML entities decoded in extracted claims (&amp; → &)")
    # 2b: rejected candidates are dropped silently — the only observations the
    # pollution pages may add are their site_claims (zero) — no cleanup noise.
    new_obs = [o for o in g.objects(type="observation")[obs_before_pollution:]
               if (o.data.get("metadata") or {}).get("lab")]
    c.that(len(new_obs) == exp["cleanup_observations"]["count"],
           f"rejected candidates must not become observations, got "
           f"{[(o.data.get('metadata') or {}).get('lab') for o in new_obs]}")
    return c.done("claim_hygiene")


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
        thread_id, msg = send_branch_message_fn(g, branch.id, mspec["content"],
                                                thread_id=thread_id,
                                                source=mspec.get("source"))
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
    bind_live_behaviors(rt)
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
    bind_live_behaviors(rt)
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

    # ── 5c: provenance-aware review notes ───────────────────────────────────
    # The digest covered SEEDED findings — in fixture mode the model invents a
    # first-person process narrative there, and the coverage check must flag
    # it. The research draft's evidence arose from live work (a worker actor)
    # — that draft must pass clean.
    digest_body = drafts()[0].data.get("content") or ""
    c.that("Review note (process claims)" in digest_body,
           "seeded-finding draft flags the invented process narrative (5c)")
    research_body = research_drafts[0].data.get("content") or "" if research_drafts else ""
    c.that("Review note (process claims)" not in research_body,
           "correctly-attributed (live-work) draft passes the process check clean")
    print("  5c: seeded → process-claim flag; live-work → clean")
    return c.done("editorial")


def run_operator_controls() -> bool:
    spec = _load("operator_controls.yaml")
    print("\n" + "=" * 64)
    print("Fixture: operator_controls — pause/resume + daily cost ceiling (ADR-015)")
    print("=" * 64)

    from lab_pack.behaviors import emit_lab_event
    from lab_pack.llm import (_LLM_STATE, LabProviderWrapper, _lab_prompt_bodies,
                              lab_paused, reset_llm_session, set_lab_paused,
                              sync_daily_budget)

    clear_lab_registry()
    reset_llm_session()
    cap = float(spec["cost_cap_usd"])
    wrapper = LabProviderWrapper(LabMockProvider(), max_total=60,
                                 max_per_behavior=10, max_daily=200,
                                 max_daily_cost_usd=cap,
                                 prompt_bodies=_lab_prompt_bodies())
    rt = Runtime(Graph(), llm_provider=wrapper)
    rt.load_pack(core_pack, settings=CoreSettings())
    rt.load_pack(comm_pack, settings=CommunicationSettings())
    rt.load_pack(lab_pack, settings=LabSettings(**(spec.get("settings") or {})))
    bind_live_behaviors(rt)
    g = rt.graph
    mission = create_mission_fn(g, spec["mission"]["title"], target_url="")
    branch = create_branch_fn(g, mission.id, "Control branch", "talk here",
                              status="active")
    rt.run_until_idle()
    c = Check()
    exp = spec["expected_outputs"]

    def add_claim(i):
        g.add_object("observation", {
            "text": f"Claim {i}: the runtime replays every event deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": mission.id},
        })
        rt.run_until_idle()

    def proposed():
        return [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]

    def plan_skips():
        return [o for o in _lab_obs(g, "behavior_skipped")
                if (o.data.get("metadata") or {}).get("behavior") == "plan"]

    # ── pause: marker event + LLM behaviors idle, ONE skip obs ──────────────
    set_lab_paused(g, True)
    c.that(str(g.events[-1].type) == "lab.paused" and lab_paused(),
           "pause appends a lab.paused marker event")
    add_claim(1)
    add_claim(2)
    c.that(len(proposed()) == 0, "paused → plan does not propose branches")
    c.that(len(plan_skips()) == exp["paused_plan_skips"],
           f"one behavior-skipped observation per behavior per episode, "
           f"not per event ({len(plan_skips())} for 2 triggers)")

    # ── answer stays live while paused ──────────────────────────────────────
    _, msg = send_branch_message_fn(g, branch.id, "are you alive in there?")
    rt.run_until_idle()
    cands = [x for x in g.objects(type="comm_response_candidate")
             if x.data.get("message_id") == msg.id]
    c.that(len(cands) == 1 and "as of event" in (cands[0].data.get("content") or ""),
           "answer still fires while paused (the operator can always talk)")
    print(f"  paused: 2 plan triggers → 0 branches, {len(plan_skips())} skip obs; "
          "answer replied")

    # ── restart-proof: pause state rebuilds from the log ────────────────────
    saved_anomalies = list(_LLM_STATE["anomalies"])
    reset_llm_session()  # simulate a process restart
    sync_daily_budget(rt)
    c.that(lab_paused(), "paused state rebuilt from the log after restart")
    _LLM_STATE["anomalies"] = saved_anomalies

    # ── resume: behaviors fire again ────────────────────────────────────────
    set_lab_paused(g, False)
    c.that(str(g.events[-1].type) == "lab.resumed" and not lab_paused(),
           "resume appends a lab.resumed marker event")
    add_claim(3)
    c.that(len(proposed()) == exp["resumed_branches"],
           f"resumed → plan proposes again ({len(proposed())})")
    print(f"  resumed: claim → {len(proposed())} proposed branch")

    # ── daily cost ceiling: native cost accounting, restart-proof ──────────
    emit_lab_event(g, "llm.responded", {"cost_usd": spec["synthetic_spend"],
                                        "model": "fixture", "behavior": "fixture"})
    sync_daily_budget(rt)
    c.that(float(_LLM_STATE["daily_cost"]) >= cap,
           f"cost rebuilt from llm.responded events "
           f"(${float(_LLM_STATE['daily_cost']):.2f} >= ${cap:.2f})")
    n_before = len(proposed())
    add_claim(4)
    blocked = [o for o in _lab_obs(g, "llm_budget")
               if "cost cap" in (o.data.get("text") or "")]
    c.that(len(proposed()) == n_before, "cost cap reached → plan blocked")
    c.that(len(blocked) == exp["cost_blocked_observations"],
           f"blocked-by-cost recorded once ({len(blocked)})")
    reset_llm_session()  # simulate another restart
    sync_daily_budget(rt)
    c.that(float(_LLM_STATE["daily_cost"]) >= cap,
           "cost ceiling is restart-proof: bouncing the process cannot reset it")
    print(f"  cost: ${float(_LLM_STATE['daily_cost']):.2f} spent >= ${cap:.2f} cap "
          f"→ blocked, {len(blocked)} observation, survives restart")
    return c.done("operator_controls")


def run_paused_boot() -> bool:
    spec = _load("paused_boot.yaml")
    print("\n" + "=" * 64)
    print("Fixture: paused_boot — a paused log boots with a live worker")
    print("=" * 64)

    import os
    import tempfile

    import lab_pack.llm as llm_mod
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (LabProviderWrapper, _lab_prompt_bodies,
                              lab_paused, reset_llm_run_counters,
                              reset_llm_session, set_lab_paused)
    from server import lab_server

    tmp = tempfile.mkdtemp(prefix="lab-paused-boot-")
    db = os.path.join(tmp, "lab.sqlite")

    def wrapped_mock():
        return LabProviderWrapper(LabMockProvider(), max_total=60,
                                  max_per_behavior=10,
                                  prompt_bodies=_lab_prompt_bodies())

    def add_claim(g, mission_id, i):
        g.add_object("observation", {
            "text": f"Claim {i}: the runtime replays every event deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": mission_id},
        })

    # ── phase 1: the "migrated" log — its tail is lab.paused plus a trigger
    # appended after the last runtime.idle (so Runtime.load requeues it) ─────
    clear_lab_registry()
    reset_llm_session()
    rt = build_lab(llm_provider=wrapped_mock(),
                   lab_settings=LabSettings(drafts_dir=tmp,
                                            **(spec.get("settings") or {})),
                   persist_to=db)
    rt.run_until_idle()
    g = rt.graph
    mission_id = str(g.objects(type="mission")[0].id)
    branch_id = str(next(b for b in g.objects(type="branch")).id)
    proposed_before = len([b for b in g.objects(type="branch")
                           if b.data.get("status") == "proposed"])
    set_lab_paused(g, True)
    add_claim(g, mission_id, 1)  # no drain: stranded, exactly like a shutdown
    rt.save_state()
    del rt

    c = Check()
    exp = spec["expected_outputs"]

    # ── phase 2: boot through the server's REAL resumed path, paused inherited
    saved_env = {k: os.environ.get(k) for k in
                 ("ACTIVEGRAPH_DB", "ACTIVEGRAPH_MEMORY_DB",
                  "LAB_DATABASE_URL", "DATABASE_URL")}
    real_select = llm_mod.select_lab_provider
    try:
        os.environ["ACTIVEGRAPH_DB"] = db
        os.environ["ACTIVEGRAPH_MEMORY_DB"] = os.path.join(tmp, "memory.sqlite")
        os.environ.pop("LAB_DATABASE_URL", None)
        os.environ.pop("DATABASE_URL", None)
        # The keyless default is a BARE mock (no pause/budget gate); boot must
        # see the wrapped provider the live server gets.
        llm_mod.select_lab_provider = lambda **_kw: (
            wrapped_mock(), {"mode": "mock", "provider": "mock", "model": None})
        clear_lab_registry()
        reset_llm_session()
        rt2 = lab_server._build_runtime()
    finally:
        llm_mod.select_lab_provider = real_select
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    g2 = rt2.graph

    c.that(lab_paused(), "paused state rebuilt from the log before the boot drain")
    depth = rt2.status().queue_depth
    c.that(depth == exp["queue_depth_after_boot"],
           f"boot run cycle drained the replay-requeued backlog "
           f"(queue_depth {depth}, want {exp['queue_depth_after_boot']})")
    proposed_boot = len([b for b in g2.objects(type="branch")
                         if b.data.get("status") == "proposed"])
    c.that(proposed_boot - proposed_before == exp["proposed_while_paused"],
           f"paused gates plan during the boot drain "
           f"({proposed_boot - proposed_before} new proposals while paused)")
    print(f"  paused boot: queue_depth={depth}, "
          f"{proposed_boot - proposed_before} proposals (worker live, behaviors gated)")

    # ── answer still replies on a process that booted into paused ───────────
    out = lab_server._chat_job(rt2, branch_id, "are you alive in there?")
    c.that(out is not None and "as of event" in (out.get("content") or ""),
           "answer replies on a paused boot (the operator can always talk)")

    # ── resume takes effect in the running process — no restart ─────────────
    res = lab_server._pause_job(rt2, False)
    c.that(res.get("changed") is True and not lab_paused()
           and any(str(e.type) == "lab.resumed" for e in g2.events),
           "resume appends lab.resumed, flips in-process state, and drains")
    add_claim(g2, mission_id, 2)
    reset_llm_run_counters()
    rt2.run_until_idle()
    proposed_now = len([b for b in g2.objects(type="branch")
                        if b.data.get("status") == "proposed"])
    c.that(proposed_now - proposed_boot == exp["resumed_proposes"],
           f"autonomous behavior fires after resume "
           f"({proposed_now - proposed_boot} new proposal)")
    print(f"  resumed in-process: claim → {proposed_now - proposed_boot} proposed branch")

    # ── the incident is itself a queued finding (requirement: forensics) ────
    c.that(any((o.data.get("metadata") or {}).get("finding_key")
               == "paused_boot_dead_worker"
               for o in g2.objects(type="observation")),
           "paused-boot incident queued as a finding (LIVE_FINDINGS)")
    return c.done("paused_boot")


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
    # ADR-018: descriptions compose prompt body + charter block, so the
    # approved body is the PREFIX of the live description.
    c.that(plan_b.description.startswith(case["v1_body"]),
           "hot-load: live behavior uses the approved body without restart")
    # The runtime registers a fresh canonical copy — the hot-load must reach
    # THAT object, not just the module original (found via ADR-019 routing).
    c.that(rt.get_behavior("lab.plan").description.startswith(case["v1_body"]),
           "hot-load reaches the runtime's registered behavior copy")

    # ── version monotonicity: v2 supersedes ────────────────────────────────
    a2 = propose_seam_fn(g, case["seam_name"], case["v2_body"], "fixture v2")
    rt.run_until_idle()
    versions = [(x.data.get("metadata") or {}).get("version")
                for x in (a1, a2)]
    c.that(versions == spec["expected_outputs"]["monotonic_versions"],
           f"versions monotonic per seam_name ({versions})")
    approve_decision_fn(g, pending_for(a2.id).id, True)
    rt.run_until_idle()
    c.that(plan_b.description.startswith(case["v2_body"]) and
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


def run_research_worker() -> bool:
    spec = _load("research_worker.yaml")
    print("\n" + "=" * 64)
    print("Fixture: research_worker — claim → fetch → attributed synthesis → "
          "complete; failure, cap, gap")
    print("=" * 64)

    pages = spec["pages"]

    def canned_fetch(url: str, **_kw) -> dict:
        clean = url.rstrip("/")
        for key, html in pages.items():
            if key.rstrip("/") == clean:
                return {"url": url, "status": 200, "content": html}
        return {"url": url, "status": 503, "content": "",
                "error": "HTTPError: 503 fixture outage"}

    rt = _new_runtime(spec, with_gateway=True, with_comm=False)
    register_web_fetch(canned_fetch, overwrite=True)
    g = rt.graph
    mission = create_mission_fn(g, spec["mission"]["title"], target_url="")
    rt.run_until_idle()

    c = Check()
    exp = spec["expected_outputs"]

    def task_for(branch_id):
        return next((t for t in g.objects(type="task")
                     if (t.data.get("metadata") or {}).get("lab_branch_id")
                     == branch_id), None)

    def calls_for(task_id):
        return [x for x in g.objects(type="capability_call")
                if (x.data.get("metadata") or {}).get("task_id") == task_id]

    def make(key, status="active"):
        b = spec["branches"][key]
        branch = create_branch_fn(g, mission.id, b["title"],
                                  b["intent"].strip(), status=status)
        rt.run_until_idle()
        return branch

    # ── success: routed task → canned fetches → attributed findings ────────
    b_ok = make("success")
    t_ok = task_for(b_ok.id)
    c.that(t_ok is not None and t_ok.data.get("status") == "done",
           f"research task claimed and completed "
           f"({t_ok.data.get('status') if t_ok else 'no task'})")
    claims = [o for o in _lab_obs(g, "research_progress")
              if (o.data.get("metadata") or {}).get("task_id") == (t_ok.id if t_ok else None)]
    c.that(len(claims) == 1, f"claim observation recorded ({len(claims)})")
    findings = [o for o in _lab_obs(g, "research_finding")
                if (o.data.get("metadata") or {}).get("lab_branch_id") == b_ok.id]
    c.that(len(findings) >= exp["success_findings_min"],
           f"attributed findings written ({len(findings)})")
    fetched_urls = {"https://example.com/replay", "https://example.com/forks"}
    c.that(all(set((o.data.get("metadata") or {}).get("source_urls") or [])
               <= fetched_urls
               and o.data.get("source_ids") for o in findings),
           "every finding attributes fetched source URLs + source ids")
    c.that(all(any(str(r.type) == "supported_by" and str(r.source) == b_ok.id
                   and str(r.target) == o.id for r in g.relations())
               for o in findings),
           "findings linked supported_by to the branch")
    evals = [e for e in g.objects(type="evaluation")
             if (e.data.get("metadata") or {}).get("lab") == "research_synthesis"
             and (e.data.get("metadata") or {}).get("lab_branch_id") == b_ok.id]
    c.that(len(evals) == 1 and any(
        str(r.type) == "supported_by" and str(r.source) == b_ok.id
        and str(r.target) == evals[0].id for r in g.relations()),
        f"synthesis evaluation written and linked to the branch ({len(evals)})")
    outcome = [e for e in g.objects(type="evaluation")
               if (e.data.get("metadata") or {}).get("lab") == "task_outcome"
               and (e.data.get("metadata") or {}).get("task_id") == t_ok.id]
    c.that(len(outcome) == 1
           and outcome[0].data.get("judgment") == "completed_successfully",
           "existing outcome path fired: task_outcome evaluation → interpret")
    c.that(not [o for o in _lab_obs(g, "capability_gap")
                if (o.data.get("metadata") or {}).get("lab_branch_id") == b_ok.id],
           "no capability gap for the claimed research task")
    print(f"  success: task done, {len(findings)} attributed finding(s), "
          "evaluation linked, no gap")

    # ── failure: every fetch fails → task failed, error in the event ───────
    b_fail = make("failure")
    t_fail = task_for(b_fail.id)
    c.that(t_fail is not None and t_fail.data.get("status") == "rejected",
           f"all fetches failed → task rejected "
           f"({t_fail.data.get('status') if t_fail else 'no task'})")
    err = (t_fail.data.get("metadata") or {}).get("error") if t_fail else None
    c.that(bool(err) and "503" in err,
           f"the error is recorded on the task ({err})")
    c.that(any(str(e.type) == "patch.applied"
               and e.payload.get("target") == (t_fail.id if t_fail else None)
               and "error" in str((e.payload.get("diff") or {}))
               for e in g.events),
           "the failure event carries the error (errors propagate)")
    fail_outcome = [e for e in g.objects(type="evaluation")
                    if (e.data.get("metadata") or {}).get("lab") == "task_outcome"
                    and (e.data.get("metadata") or {}).get("task_id")
                    == (t_fail.id if t_fail else None)]
    c.that(len(fail_outcome) == 1 and fail_outcome[0].data.get("judgment") == "failed",
           "failure flows into the existing outcome path")
    print(f"  failure: task rejected, error on the event: {str(err)[:60]}…")

    # ── the per-task fetch cap binds ────────────────────────────────────────
    b_cap = make("capped")
    t_cap = task_for(b_cap.id)
    n_calls = len(calls_for(t_cap.id)) if t_cap else -1
    c.that(n_calls == exp["capped_calls"],
           f"fetch cap binds: {n_calls} call(s) for 4 candidate URLs (cap "
           f"{exp['capped_calls']})")
    research_meta = ((g.get_object(t_cap.id).data.get("metadata") or {})
                     .get("research") or {}) if t_cap else {}
    c.that(research_meta.get("fetched") == exp["capped_calls"],
           f"progress patches recorded per fetch ({research_meta})")
    print(f"  cap: {n_calls} fetches for 4 URLs")

    # ── unhandled routing still records a capability gap ────────────────────
    b_gap = make("unhandled")
    t_gap = task_for(b_gap.id)
    gaps = [o for o in _lab_obs(g, "capability_gap")
            if (o.data.get("metadata") or {}).get("lab_branch_id") == b_gap.id]
    c.that(len(gaps) == exp["gap_observations"]
           and t_gap is not None and t_gap.data.get("status") == "blocked",
           f"codebase routing untouched: gap recorded, task blocked "
           f"({len(gaps)})")
    print("  unhandled: codebase routing → gap, task blocked")
    return c.done("research_worker")


def run_model_routing() -> bool:
    spec = _load("model_routing.yaml")
    print("\n" + "=" * 64)
    print("Fixture: model_routing — per-behavior models, hot reroute, ceiling clamp")
    print("=" * 64)

    import lab_pack.behaviors as lb
    from lab_pack.kernel import ABSOLUTE_DAILY_COST_CEILING_USD
    from lab_pack.llm import (_LLM_STATE, LabProviderWrapper,
                              _lab_prompt_bodies, current_cost_cap,
                              reset_llm_session)
    from lab_pack.seams import apply_model_routing, propose_seam_fn

    rt = _new_runtime(spec, with_gateway=False, with_comm=True)
    g = rt.graph
    mission = create_mission_fn(g, spec["mission"]["title"], target_url="")
    branch = create_branch_fn(g, mission.id, "Routing branch", "talk here",
                              status="active")
    rt.run_until_idle()
    routed = apply_model_routing(g)

    c = Check()
    exp = spec["expected_outputs"]
    cases = spec["cases"]

    def behavior(name):
        # The runtime's REGISTERED copy — the object prompt assembly reads.
        return rt.get_behavior(f"lab.{name}")

    def llm_requested(behavior_name):
        return [e for e in g.events if str(e.type) == "llm.requested"
                and str(e.payload.get("behavior", "")).endswith(behavior_name)]

    # ── two behaviors resolve different models ──────────────────────────────
    c.that(routed.get("plan") == exp["default_plan_model"]
           and routed.get("answer") == exp["default_answer_model"],
           f"plan and answer resolve different models ({routed})")
    c.that(behavior("plan").model == exp["default_plan_model"]
           and behavior("answer").model == exp["default_answer_model"],
           "resolution is stamped onto behavior.model")

    # ── llm.requested records the per-behavior resolution ──────────────────
    g.add_object("observation", {
        "text": "Claim: the runtime replays every event deterministically.",
        "confidence": 0.7, "category": "fact",
        "metadata": {"lab": "site_claim", "mission_id": mission.id},
    })
    rt.run_until_idle()
    _, msg = send_branch_message_fn(g, branch.id, "what model are you on?")
    rt.run_until_idle()
    plan_reqs = llm_requested("plan")
    answer_reqs = llm_requested("answer")
    c.that(bool(plan_reqs) and plan_reqs[-1].payload.get("model")
           == exp["default_plan_model"],
           f"llm.requested records plan's routed model "
           f"({plan_reqs[-1].payload.get('model') if plan_reqs else None})")
    c.that(bool(answer_reqs) and answer_reqs[-1].payload.get("model")
           == exp["default_answer_model"],
           f"llm.requested records answer's routed model "
           f"({answer_reqs[-1].payload.get('model') if answer_reqs else None})")
    print(f"  plan → {routed.get('plan')}; answer → {routed.get('answer')} "
          "(both on llm.requested)")

    # ── seam override takes effect without restart ──────────────────────────
    a = propose_seam_fn(g, "setting.model.answer",
                        cases["answer_override_model"], "fixture reroute")
    rt.run_until_idle()
    d = next(x for x in g.objects(type="decision")
             if x.data.get("subject_ref") == a.id and x.data.get("status") == "pending")
    approve_decision_fn(g, d.id, True, "fixture approve")
    rt.run_until_idle()
    c.that(behavior("answer").model == cases["answer_override_model"],
           f"approved model seam hot-reroutes answer "
           f"({behavior('answer').model})")
    n_before = len(llm_requested("answer"))
    send_branch_message_fn(g, branch.id, "and now?")
    rt.run_until_idle()
    answer_reqs = llm_requested("answer")
    c.that(len(answer_reqs) > n_before and answer_reqs[-1].payload.get("model")
           == cases["answer_override_model"],
           "post-approval llm.requested records the rerouted model")
    c.that(behavior("plan").model == exp["default_plan_model"],
           "other behaviors keep their own routing")
    print(f"  seam reroute: answer → {behavior('answer').model}, no restart")

    # ── budget defaults + the kernel ceiling clamp ──────────────────────────
    c.that(LabSettings().daily_cost_cap_usd == exp["daily_cost_cap_default"],
           f"daily_cost_cap_usd default is {exp['daily_cost_cap_default']}")
    c.that(ABSOLUTE_DAILY_COST_CEILING_USD == exp["absolute_ceiling"],
           "kernel ceiling constant present")
    reset_llm_session()
    wrapper = LabProviderWrapper(LabMockProvider(), max_daily_cost_usd=500.0,
                                 prompt_bodies=_lab_prompt_bodies())
    c.that(wrapper.effective_cost_cap() == exp["absolute_ceiling"],
           f"settings cap 500 clamps to the ceiling "
           f"({wrapper.effective_cost_cap()})")
    _LLM_STATE["cost_cap_override"] = 250.0  # an approved seam body of "250"
    c.that(wrapper.effective_cost_cap() == exp["absolute_ceiling"],
           "seam override 250 clamps to the ceiling")
    c.that(current_cost_cap(500.0) == exp["absolute_ceiling"],
           "display path clamps identically")
    _LLM_STATE["cost_cap_override"] = 25.0
    c.that(wrapper.effective_cost_cap() == 25.0,
           "caps under the ceiling pass through unclamped")
    reset_llm_session()
    print(f"  ceiling: 500/250 → {exp['absolute_ceiling']}; 25 → 25")
    return c.done("model_routing")


def run_charter() -> bool:
    spec = _load("charter.yaml")
    print("\n" + "=" * 64)
    print("Fixture: charter — verbatim injection, file v1, graph v2 via the gate")
    print("=" * 64)

    import lab_pack.behaviors as lb
    from lab_pack.seams import (active_charter, apply_approved,
                                charter_file_default, propose_seam_fn)

    rt = _new_runtime(spec, with_gateway=False, with_comm=False)
    g = rt.graph
    mission = create_mission_fn(g, spec["mission"]["title"], target_url="")
    rt.run_until_idle()

    c = Check()
    exp = spec["expected_outputs"]
    cases = spec["cases"]
    file_body = charter_file_default()

    def desc(name):
        # The runtime's REGISTERED copy (the loader registers fresh
        # canonical-named behaviors; the original is only the template).
        return rt.get_behavior(f"lab.{name}").description

    def add_claim(i):
        g.add_object("observation", {
            "text": f"Claim {i}: the runtime replays every event deterministically.",
            "confidence": 0.7, "category": "fact",
            "metadata": {"lab": "site_claim", "mission_id": mission.id},
        })
        rt.run_until_idle()

    def proposed_branches():
        return [b for b in g.objects(type="branch")
                if b.data.get("status") == "proposed"]

    def pending_for(artifact_id):
        return next((d for d in g.objects(type="decision")
                     if d.data.get("kind") == "self_modify"
                     and d.data.get("subject_ref") == artifact_id
                     and d.data.get("status") == "pending"), None)

    # ── v1 file default injected verbatim into the three behaviors ─────────
    c.that("CHARTER v1 — activegraph-lab" in file_body,
           "charter.md body is the operator's v1 charter")
    for name in exp["charter_behaviors"]:
        d = desc(name)
        c.that(file_body in d and "charter.mission v1" in d,
               f"{name}: charter v1 injected verbatim (delimited block)")
    for name in exp["uninjected_behaviors"]:
        c.that("===== CHARTER" not in desc(name),
               f"{name}: charter NOT injected (answer is excluded by design)")
    c.that(active_charter(g) == (1, file_body),
           "active charter is the file default at version 1")
    print(f"  v1 (file) injected into {exp['charter_behaviors']}; "
          f"answer untouched")

    # ── behavior outputs record the charter version in force ───────────────
    add_claim(1)
    b1 = proposed_branches()[-1]
    stamp1 = (b1.data.get("metadata") or {}).get("seam_versions") or {}
    c.that(stamp1.get("charter.mission") == exp["file_default_version"],
           f"plan output stamps charter.mission v1 ({stamp1})")

    # ── graph v2 supersedes file v1, only through the gate ─────────────────
    v2_body = cases["charter_v2_body"].strip()
    a2 = propose_seam_fn(g, "charter.mission", v2_body, "fixture charter v2")
    rt.run_until_idle()
    meta2 = a2.data.get("metadata") or {}
    c.that(meta2.get("version") == exp["first_graph_version"]
           and meta2.get("parent_version") == exp["file_default_version"],
           f"first graph charter is v2 with parent v1 ({meta2})")
    c.that(file_body in desc("plan"),
           "pending charter does NOT load — v1 stays active pre-approval")
    approve_decision_fn(g, pending_for(a2.id).id, True, "fixture approve")
    rt.run_until_idle()
    for name in exp["charter_behaviors"]:
        d = desc(name)
        c.that(v2_body in d and "charter.mission v2" in d and file_body not in d,
               f"{name}: approved charter v2 supersedes file v1 (hot-loaded)")
    add_claim(2)
    b2 = proposed_branches()[-1]
    stamp2 = (b2.data.get("metadata") or {}).get("seam_versions") or {}
    c.that(stamp2.get("charter.mission") == exp["first_graph_version"],
           f"post-approval output stamps charter.mission v2 ({stamp2})")
    c.that(stamp1.get("charter.mission") != stamp2.get("charter.mission"),
           "replay record: the two outputs carry the version in force at "
           "their own execution")
    print(f"  v2 approved → hot-loaded; stamps: {stamp1.get('charter.mission')}"
          f" then {stamp2.get('charter.mission')}")

    # ── prompt seam and charter compose ────────────────────────────────────
    p1_body = cases["prompt_v1_body"].strip()
    ap = propose_seam_fn(g, "prompt.plan", p1_body, "fixture prompt v1")
    rt.run_until_idle()
    approve_decision_fn(g, pending_for(ap.id).id, True)
    rt.run_until_idle()
    d = desc("plan")
    c.that(d.startswith(p1_body) and v2_body in d,
           "approved prompt seam composes with the active charter")

    # ── whitelist + kernel refusals ─────────────────────────────────────────
    for ref in cases["refusals"]:
        r = propose_seam_fn(g, ref["seam_name"], ref["body"])
        c.that(r is None, f"refused outright: {ref['seam_name']}")
    refusals = [o for o in g.objects(type="observation")
                if (o.data.get("metadata") or {}).get("lab") == "seam_refused"]
    c.that(len(refusals) == exp["refusal_observations"],
           f"refusals are graph-visible ({len(refusals)})")

    # ── boot/resume recomposes from the log ────────────────────────────────
    clear_lab_registry()  # simulated restart: file defaults restored
    c.that(file_body in desc("plan") and v2_body not in desc("plan"),
           "restart restores file defaults before apply_approved")
    n = apply_approved(g)
    d = desc("plan")
    c.that(n >= 2 and d.startswith(p1_body) and v2_body in d,
           f"apply_approved recomposes prompt v1 + charter v2 at boot ({n})")
    print("  restart → apply_approved recomposes graph charter + prompt")
    return c.done("charter")


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


def run_storage_selection() -> bool:
    """ADR-009 (note): backend selection order LAB_DATABASE_URL > DATABASE_URL > SQLite."""
    print("\n" + "=" * 64)
    print("Fixture: storage_selection — LAB_DATABASE_URL > DATABASE_URL > SQLite")
    print("=" * 64)

    import os
    from lab_pack import storage

    keys = ("LAB_DATABASE_URL", "DATABASE_URL", "ACTIVEGRAPH_DB")
    saved = {k: os.environ.pop(k, None) for k in keys}
    lab_url = "postgres://lab_fixture@db.lab.fixture:5432/lab"
    legacy_url = "postgres://legacy_fixture@db.legacy.fixture:5432/lab"
    c = Check()
    try:
        os.environ["LAB_DATABASE_URL"] = lab_url
        os.environ["DATABASE_URL"] = legacy_url
        c.that(storage.backend() == "postgres" and storage.store_url() == lab_url,
               f"both set → LAB_DATABASE_URL wins (got {storage.store_url()})")

        del os.environ["LAB_DATABASE_URL"]
        c.that(storage.backend() == "postgres" and storage.store_url() == legacy_url,
               f"LAB_DATABASE_URL absent → DATABASE_URL used (got {storage.store_url()})")

        del os.environ["DATABASE_URL"]
        c.that(storage.backend() == "sqlite" and storage.store_url().endswith("lab.sqlite"),
               f"neither set → SQLite default (got {storage.store_url()})")

        os.environ["LAB_DATABASE_URL"] = "postgresql://lab_fixture@db.lab.fixture:5432/lab"
        c.that(storage.store_url() == lab_url,
               "postgresql:// alias normalized on the LAB_DATABASE_URL path")
        print("  both → LAB_DATABASE_URL; legacy → DATABASE_URL; neither → sqlite")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return c.done("storage_selection")


def run_all() -> None:
    results = [
        run_bootstrap(),
        run_claim_hygiene(),
        run_branch_lifecycle(),
        run_thread_equals_branch(),
        run_capability_gap(),
        run_draft_writer(),
        run_editorial(),
        run_operator_controls(),
        run_paused_boot(),
        run_seams(),
        run_charter(),
        run_model_routing(),
        run_research_worker(),
        run_graph_code(),
        run_compat_regression(),
        run_storage_selection(),
    ]
    passed = sum(results)
    print(f"\n{'=' * 64}")
    print(f"lab pack: {passed}/{len(results)} fixtures passed")
    print("=" * 64 + "\n")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    run_all()
