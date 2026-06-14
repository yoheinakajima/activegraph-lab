"""build_lab() — compose the lab from activegraph-packs + lab_pack.

A bundle is a load order plus a factory with good defaults — not a pack,
no ontology (packs-repo convention). build_lab() loads the upstream packs
from the pinned activegraph-packs dependency, loads lab_pack, registers the
web fetcher with tool_gateway, and creates the mission for
https://activegraph.ai with the read_the_website seed branch.
"""

from __future__ import annotations

from typing import Any, Optional

from activegraph import Graph, Runtime

from packs.core import pack as core_pack, CoreSettings
from packs.tool_gateway import pack as tool_gateway_pack, ToolGatewaySettings
from packs.secrets import pack as secrets_pack, SecretsSettings
from packs.memory_gateway import pack as memory_gateway_pack, MemoryGatewaySettings
from packs.agent_profile import pack as agent_profile_pack, AgentProfileSettings
from packs.identity_auth import pack as identity_auth_pack, IdentitySettings
from packs.communication import pack as communication_pack, CommunicationSettings
from packs.chat import pack as chat_pack, ChatSettings
from packs.research import pack as research_pack, ResearchSettings
from packs.codebase import pack as codebase_pack, CodebaseSettings

from . import pack as lab_pack
from .settings import LabSettings
from .tools import create_branch_fn, create_mission_fn, register_web_fetch

MISSION_TITLE = "Grow activegraph.ai's evidence base"
MISSION_STATEMENT = (
    "Crawl activegraph.ai, infer what the project claims, find the gaps "
    "between claims and linked evidence, propose branches that close them, "
    "and gate everything that publishes or self-modifies."
)
MISSION_URL = "https://activegraph.ai"
SEED_BRANCH_TITLE = "read_the_website"
SEED_BRANCH_INTENT = (
    "Read activegraph.ai end to end and turn every unevidenced claim into "
    "a proposed branch with the gap recorded as evidence."
)

LAB_BUNDLE = [
    core_pack,
    tool_gateway_pack,
    secrets_pack,
    memory_gateway_pack,
    agent_profile_pack,
    identity_auth_pack,
    communication_pack,
    chat_pack,
    research_pack,
    codebase_pack,
    lab_pack,
]


def build_lab(
    *,
    llm_provider: Any = None,
    lab_settings: Optional[LabSettings] = None,
    memory_backend_url: str = ":memory:",
    persist_to: Optional[str] = None,
    create_mission: bool = True,
    fetch_handler: Any = None,
    graph: Optional[Graph] = None,
) -> Runtime:
    """Create a Runtime with the full lab bundle loaded and branch zero seeded.

    Args:
        llm_provider: provider for the lab's and chat's llm_behaviors
            (lab_pack.llm.select_lab_provider() picks one from the env).
        lab_settings: override LabSettings.
        memory_backend_url: memory_gateway backend (file path for durability).
        persist_to: SQLite event-log path for Runtime persistence.
        create_mission: seed the activegraph.ai mission + read_the_website
            branch (skip when resuming from a persisted log).
        fetch_handler: web fetch override (fixtures/canned pages); default is
            the live urllib fetcher.
        graph: pre-built Graph (defaults to a fresh one).
    """
    kwargs: dict[str, Any] = {}
    if llm_provider is not None:
        kwargs["llm_provider"] = llm_provider
    if persist_to is not None:
        kwargs["persist_to"] = persist_to

    rt = Runtime(graph or Graph(), **kwargs)
    load_lab_packs(rt, lab_settings=lab_settings, memory_backend_url=memory_backend_url)
    register_web_fetch(fetch_handler)
    from .github_read import register_github_read
    register_github_read()  # ADR-022 rung 1: read-only, allowlisted

    if create_mission:
        mission = create_mission_fn(
            rt.graph, MISSION_TITLE, MISSION_STATEMENT, MISSION_URL
        )
        seed = create_branch_fn(
            rt.graph, mission.id, SEED_BRANCH_TITLE, SEED_BRANCH_INTENT,
            status="active", authority="gated",
        )
        _seed_upstream_friction(rt.graph, mission.id, seed.id)
    return rt


def _seed_upstream_friction(graph, mission_id: str, branch_id: str) -> None:
    """ADR-005: friction consuming activegraph-packs is evidence. Recorded as
    an observation under the mission and a draft upstream-issue artifact —
    never as a direct edit to the packs repo. The artifact stays draft:
    publishing it is gated like everything else."""
    obs = graph.add_object("observation", {
        "text": (
            "Upstream friction: the packs repo is split on add_relation argument "
            "order — core/research/tool_gateway call it type-first while chat "
            "follows the real (source, target, type) signature, so a composed "
            "graph holds both encodings and view traversal only follows the "
            "signature-order ones. The lab writes signature-order and decodes "
            "both ('#' discriminator) in its feed."
        ),
        "confidence": 0.95,
        "category": "risk",
        "metadata": {"lab": "upstream_friction", "mission_id": mission_id,
                     "subject": "activegraph-packs"},
    })
    graph.add_relation(branch_id, obs.id, "supported_by")
    artifact = graph.add_object("artifact", {
        "kind": "issue_draft",
        "title": "activegraph-packs: unify add_relation call convention",
        "content": (
            "Proposed upstream issue (draft — publishing is gated):\n\n"
            "Packs disagree on add_relation argument order. core, research and "
            "tool_gateway pass the relation type first; chat passes (source, "
            "target, type) per the Graph.add_relation signature. Relations "
            "written type-first are invisible to view traversal and "
            "get_relations, and the Inspector's serializer assumes the "
            "type-first encoding, so mixed graphs decode inconsistently. "
            "Suggest standardizing on the signature order and updating the "
            "Inspector decode.\n\n"
            "The lab's interim handling is ADR-008 (docs/DECISIONS.md): write "
            "signature-order everywhere, decode both encodings through "
            "lab_pack/compat.py until upstream standardizes.\n\n"
            "Evidence: " + obs.id
        ),
        "format": "markdown",
        "status": "draft",
        "observation_ids": [obs.id],
        "metadata": {"lab": "upstream_issue", "repo": "activegraph-packs"},
    })
    graph.add_relation(branch_id, artifact.id, "produced")
    _seed_findings(graph, mission_id, branch_id, friction_obs_id=obs.id,
                   issue_artifact_id=artifact.id)


def _seed_findings(graph, mission_id: str, branch_id: str, *,
                   friction_obs_id: str, issue_artifact_id: str) -> None:
    """B3: the build already produced three findings worth writing up.
    Tagging them metadata.finding=true makes them draft_writer fuel — each
    yields a blog_draft artifact + a pending publish decision (gated)."""
    findings = [
        (
            "Finding: the activegraph-packs repo is split on add_relation "
            "argument order — core/research/tool_gateway write the relation "
            "type into the `source` field while chat follows the real "
            "(source, target, type) signature. View traversal only follows "
            "signature-order relations, so the encodings are not equivalent. "
            "The lab writes signature-order and decodes both (ADR-008).",
            [friction_obs_id, issue_artifact_id],
        ),
        (
            "Finding: emergent work dispatch hit a real capability gap — at "
            "pin da2bca77, no research or codebase pack behavior reacts to "
            "core task objects; only team_ops watches tasks. Every lab "
            "dispatch therefore records a capability-gap observation, which "
            "is the honest state of the worker ecosystem, not an error.",
            [],
        ),
        (
            "Finding: cross-repo entry-point discovery works — pip-installing "
            "activegraph-packs from a pinned git SHA exposes all 17 packs via "
            "activegraph.packs discover()/load_by_name, and this lab's own "
            "pack registers the same way from a separate repo. The lab is the "
            "first external consumer of the packs conventions.",
            [],
        ),
        (
            "Finding: the runtime's restricted BehaviorGraph (no collection "
            "scans inside behaviors) forced the seam loader to resolve "
            "cache-only in behavior context — and that constraint turned out "
            "to be a security property: the cache is populated exclusively by "
            "gate-driven hot_load and boot-time apply_approved, so a seam "
            "cannot become active on any path that bypasses the gate.",
            [],
        ),
        (
            "Finding: the runtime logs llm.requested BEFORE the provider "
            "executes, so budget-blocked attempts are in the event log too. "
            "Rebuilding the daily LLM cap from the log therefore counts "
            "blocked attempts — the cap survives restarts and cannot be "
            "reset by bouncing the process. An accident of event ordering "
            "that behaves like a designed safety feature.",
            [],
        ),
        (
            "Finding: the restricted BehaviorGraph exposes no relation "
            "iteration, so the lab's old 'decided branch with >=2 evidence "
            "emits a finding' path could NEVER fire from inside the gate "
            "behavior — _branch_evidence_ids swallowed the AttributeError "
            "and returned an empty list, silently. Only seeded findings ever "
            "drove drafting. Discovered while wiring the ADR-014 research "
            "threshold, which made the dead path load-bearing; fixed with a "
            "registry fed by relation.created events and rebuilt from the "
            "graph on resume. A try/except around a capability probe turned "
            "a missing API into invisible policy.",
            [],
        ),
        (
            "Finding: the lab's first live crawl recorded raw fetch JSON "
            "envelopes and nav/SVG markup as site 'claims' — one claim in the "
            "public log literally begins '{\"url\": \"https://activegraph.ai\", "
            "\"status\": 200…'. The old extractor accepted any 30+-char "
            "sentence containing a cue word OR a digit, and envelope JSON and "
            "SVG path data are full of both. Fixed by stripping non-content "
            "subtrees (script/style/svg/nav/footer, entities decoded) and "
            "putting a shape gate in front of the cue check: candidates must "
            "be length-bounded, sentence-terminated, mostly real words, never "
            "parseable as JSON, no markup characters. Rejected candidates are "
            "dropped silently — logging the cleanup would be its own "
            "pollution. Deterministic extraction needs a shape gate, not just "
            "cue words.",
            [],
        ),
    ]
    for text, extra_refs in findings:
        f = graph.add_object("observation", {
            "text": text,
            "confidence": 0.9,
            "category": "fact",
            "metadata": {"lab": "finding", "finding": True,
                         "lab_branch_id": branch_id, "mission_id": mission_id,
                         "evidence_refs": extra_refs},
        })
        graph.add_relation(branch_id, f.id, "supported_by")
    queue_findings_once(graph, branch_id=branch_id, mission_id=mission_id)


# Findings discovered after first deploy. The log is the only persistence
# and _seed_findings never re-runs on a resumed runtime, so these can only
# ever be APPENDED — keyed so the boot backfill is idempotent.
LIVE_FINDINGS: list[dict] = [
    {
        "key": "mcp_send_chat_predicate_gap",
        "text": (
            "Finding: the first external MCP session surfaced a reply gap in "
            "send_chat — operator messages tagged source=operator_via_mcp "
            "landed in the public log (event_count advanced) but drew no "
            "reply, and the tool returned a generic error. The lab's own "
            "llm.requested-before-execution property was the diagnostic: "
            "llm_calls_today stayed flat, and since blocked attempts log "
            "BEFORE the provider runs, a flat counter means the answer "
            "behavior never fired at all — predicate/dispatch territory, not "
            "budget, parse, or provider failure. Fix: the answer subscription "
            "matches operator authority (the server-stamped sender), never "
            "the literal source tag, and send_chat now returns a structured "
            "partial success (message event ids + reply-pending) instead of "
            "a generic error when only the reply phase fails."
        ),
    },
    {
        "key": "paused_boot_dead_worker",
        "text": (
            "Finding: a process that inherited paused=true from the migrated "
            "log booted with a dead worker. The resumed-boot path only drained "
            "the runtime when findings were backfilled, so the replay-requeued "
            "backlog (every event after the log's last runtime.idle — here the "
            "pre-migration lab.paused at evt_1702) sat parked from boot "
            "(evt_1845) onward; the operator's resume (evt_1846) appended a "
            "marker no run cycle picked up, and the next message (evt_1847) "
            "drew no reply and no llm.requested, while a process that booted "
            "unpaused had answered the identical message in seconds the same "
            "day (evt_1590→evt_1616). Diagnosed entirely from public log "
            "forensics over MCP. Fix: the boot run cycle always happens and "
            "resume drains immediately — paused gates which behaviors fire "
            "(everything but answer idles), never whether the worker runs."
        ),
    },
    {
        "key": "emit_projects_before_append",
        "text": (
            "Finding (upstream, activegraph core): Graph.emit projects an "
            "event to the in-memory log — and serves it from every "
            "projection — BEFORE store.append runs, and swallows store "
            "failures, so a wedged store leaves the runtime confidently "
            "serving phantom state. This lab ran NON-DURABLE in production "
            "for two days because of that ordering: a pg_restore'd lineage "
            "left the events.seq sequence behind the restored rows, every "
            "durable append died with a UniqueViolation inside graph.emit "
            "AFTER the event entered memory, and all projections (including "
            "MCP forensics) kept reporting the writes as committed "
            "(ADR-023; the evt evidence is in the log). Proposed upstream "
            "change: surface append failures loudly — fail the emit, or "
            "mark the runtime degraded so health checks and projections can "
            "say so. Draft fuel and an upstream issue candidate alongside "
            "the add_relation convention finding."
        ),
    },
    {
        "key": "store_immortal_connection",
        "text": (
            "Finding (upstream, activegraph core): PostgresEventStore "
            "assumes an immortal connection — a URL target opens one "
            "dedicated connection at construction and never reconnects — "
            "while serverless Postgres guarantees the opposite: Neon "
            "suspends an idle compute and kills its connections. Observed "
            "twice in production with the identical signature: the first "
            "write after an idle suspend fails AdminShutdown ('terminating "
            "connection due to administrator command'), every subsequent "
            "write fails OperationalError ('the connection is closed') "
            "until a process restart. ADR-023 surfaced both correctly; "
            "nothing committed. The lab works around it in its storage "
            "adapter (reconnect + retry-exactly-once on connection-class "
            "errors, never on constraint violations, each reconnect on the "
            "diagnostics ring buffer). Proposed upstream change: "
            "reconnect-with-bounded-retry belongs in the store itself — "
            "any URL-target store on a serverless backend hits this."
        ),
    },
    {
        "key": "model_parameter_compatibility",
        "text": (
            "Finding: the first Opus-routed call surfaced a model-parameter "
            "compatibility hazard — ADR-019 routing seams can point a "
            "behavior at a model the call path can't speak to. The lab's "
            "behavior declarations hardcoded temperature=0.2-0.4; the "
            "routed model rejects any temperature but the default ('400: "
            "temperature may only be set to 1'), and the failure was "
            "misfiled as llm_parse_failure because every provider exception "
            "carried the single 'parse' label (observation#142, evt_2870). "
            "Fix: the lab sets no temperature unless explicitly chosen — a "
            "framework-default value is forwarded as the server default, "
            "the wire-equivalent of omitting the field through the pinned "
            "providers (ADR-005) — and a 400 naming an unsupported or "
            "deprecated parameter strips it and retries exactly once, "
            "recording the strip on the llm.responded payload "
            "(provider_meta.lab_param_stripped). Failure domains split: "
            "llm_call_failure for provider/API/network errors (no model "
            "output exists), llm_parse_failure reserved for output that "
            "arrived but didn't parse. Upstream candidate: the pinned "
            "providers serialize temperature unconditionally, so true "
            "omission is impossible below the lab — parameter-compatibility "
            "handling belongs next to the HTTP assembly in the provider."
        ),
    },
    {
        "key": "event_burst_budget_starvation",
        "text": (
            "Finding: the 2026-06-12 19:24–19:30 burst grew the log from "
            "4,357 to 13,677 events in ~15 minutes, roughly 78% of it "
            "no-op behavior bookkeeping — caused_by fan-out turned single "
            "triggers into event cascades, and MCP reply timeouts arrived "
            "as collateral (every projection walks the whole log). The "
            "budget rails held: spend stayed capped. But they starved "
            "silently — lab.plan went [lab-inert] on the per-behavior cap "
            "with NO observation, because per-behavior exhaustion shared "
            "the session-wide budget-recorded flag and any earlier budget "
            "observation swallowed it, so a newly activated branch's "
            "planning silently no-op'd. Fixed in this lab: per-behavior "
            "exhaustion records one observation per behavior per run "
            "episode plus a feed narration, mirroring the total-budget "
            "path. Upstream-relevant: event-log growth under behavior "
            "fan-out is a runtime-shaped cost every ActiveGraph "
            "deployment will meet. Debounce/compaction design is "
            "deliberately NOT decided here — it is reserved for the "
            "lab's own investigation branch."
        ),
    },
    {
        # observation#1046 — the canonical first case for the self-repair loop
        # (ADR-036). A code defect about the lab's OWN code, tagged so the
        # planner can self-dispatch a gated code-fix branch for it, no operator
        # authoring. The fix harness is the lab's own fixture suite (a
        # research-worker source-ordering fix must keep every fixture green).
        "key": "research_source_selection_starves_operator_urls",
        "text": (
            "Finding (lab code defect): the research worker's source selection "
            "starves operator-named sources under a tight fetch cap. "
            "_source_urls (lab_pack/research_worker.py) seeds its candidate "
            "list with operator_direction URLs FIRST (deliberately, so the cap "
            "cannot starve them), then appends the derived defaults — the "
            "branch claim URL and the mission target_url — and ONLY THEN the "
            "URLs the operator wrote into the task description / activation "
            "message (the `for t in texts` tail). So a URL the operator names "
            "inline ('verify this against https://… ') sorts BEHIND the "
            "mission homepage and the claim URL, and when research_fetch_cap is "
            "small it is dropped before it is ever fetched — the inverse of the "
            "documented intent that 'the operator can steer sources by "
            "mentioning links'. The bug is an ordering one: only the "
            "operator_direction channel is prioritized; the description / "
            "activation-message channel the docstring also promises is not. "
            "Fix shape: gather ALL operator-supplied URLs (direction + "
            "description + activation message) ahead of the derived defaults, "
            "so explicitly named sources always survive the cap; lock it with "
            "a research_worker fixture asserting an inline-named URL is fetched "
            "even when the cap would otherwise fill on defaults."
        ),
        "metadata": {
            # ADR-036 self-repair tags: an OPEN code defect about the lab's own
            # repo, with a proving harness. The planner self-dispatches a gated
            # code-fix branch from this — no operator hands it the bug.
            "code_defect": True,
            "repo": "yoheinakajima/activegraph-lab",
            "fix_command": "python -m lab_pack.fixtures.run_fixtures",
        },
    },
    {
        "key": "accident_became_policy_pinned",
        "text": (
            "Finding: two of the lab's safety properties began life as "
            "beneficial ACCIDENTS — the published posts described them as "
            "durable features, but a beneficial accident is one refactor from "
            "gone unless a test pins it. Both are now pinned by named "
            "regression tests (Phase 3). (1) The daily budget cap rebuilds "
            "correctly across a restart WITH blocked attempts counted: a "
            "cost-capped LLM attempt is logged as an llm.requested event "
            "before the provider returns inert, and sync_daily_budget rebuilds "
            "the used-count from those log events — not the in-session "
            "counter, which never increments for blocked calls — so bouncing "
            "the process cannot reset the cap (fixture budget_cap_restart). "
            "(2) A seam cannot activate except through a gate-approved "
            "hot-load: a proposed-but-unapproved seam is invisible to "
            "resolution (full-graph scan honors only status=approved; "
            "behaviors resolve cache-only, and the cache is written only by "
            "hot_load on approval and apply_approved at boot) — it stays inert "
            "through proposal, the gate's approval-request, and a simulated "
            "boot, going live ONLY on approval (fixture seam_no_bypass). Both "
            "properties HELD under test; the tests now keep them honest."
        ),
    },
]


def queue_findings_once(graph, *, branch_id: str, mission_id: str) -> int:
    """Append any LIVE_FINDINGS missing from the graph (dedup by
    metadata.finding_key). Called from _seed_findings on fresh builds and
    from the server at boot on resumed ones. Returns the number appended."""
    present = {(o.data.get("metadata") or {}).get("finding_key")
               for o in graph.objects(type="observation")}
    appended = 0
    for finding in LIVE_FINDINGS:
        if finding["key"] in present:
            continue
        meta = {"lab": "finding", "finding": True,
                "finding_key": finding["key"],
                "lab_branch_id": branch_id, "mission_id": mission_id,
                "evidence_refs": []}
        # A finding may carry extra metadata — notably the ADR-036 self-repair
        # tags (code_defect / repo / fix_command / fix_diff) that let the
        # planner self-dispatch a gated code-fix branch for it.
        meta.update(finding.get("metadata") or {})
        f = graph.add_object("observation", {
            "text": finding["text"],
            "confidence": 0.9,
            "category": "fact",
            "metadata": meta,
        })
        graph.add_relation(branch_id, f.id, "supported_by")
        appended += 1
    return appended


def load_lab_packs(
    rt: Runtime,
    *,
    lab_settings: Optional[LabSettings] = None,
    memory_backend_url: str = ":memory:",
) -> None:
    """Load the bundle's packs onto an existing Runtime (also used on resume —
    Runtime.load replays state without behaviors, so packs re-register here)."""
    rt.load_pack(core_pack, settings=CoreSettings())
    # max_output_chars sized so a full fetch envelope survives storage: the
    # lab's fetcher returns up to 200K chars of page and JSON-escaping
    # inflates it (~2x worst case for markup-heavy HTML). The gateway's 10K
    # default truncated the live homepage's envelope mid-string — the 1/30
    # crawl stall; diagnosis above _parse_fetch_envelope in behaviors.py,
    # which also salvages any envelope that overflows this bound anyway.
    rt.load_pack(tool_gateway_pack,
                 settings=ToolGatewaySettings(max_output_chars=450_000))
    rt.load_pack(secrets_pack, settings=SecretsSettings())
    rt.load_pack(memory_gateway_pack, settings=MemoryGatewaySettings(backend_url=memory_backend_url))
    rt.load_pack(agent_profile_pack, settings=AgentProfileSettings())
    rt.load_pack(identity_auth_pack, settings=IdentitySettings())
    rt.load_pack(communication_pack, settings=CommunicationSettings())
    rt.load_pack(chat_pack, settings=ChatSettings(memory_backend_url=memory_backend_url))
    rt.load_pack(research_pack, settings=ResearchSettings())
    rt.load_pack(codebase_pack, settings=CodebaseSettings())
    rt.load_pack(lab_pack, settings=lab_settings or LabSettings())
    # The loader registered fresh canonical copies of the lab behaviors;
    # bind them so seam hot-loads and model routing reach the live runtime.
    from .behaviors import bind_live_behaviors
    bind_live_behaviors(rt)
    # ADR-019: stamp the per-behavior model resolution onto the live
    # behaviors (the runtime records behavior.model on llm.requested).
    # Resumed boots re-stamp with seam overrides via seams.apply_approved.
    from .llm import active_provider
    from .seams import apply_model_routing
    apply_model_routing(rt.graph, active_provider() or rt.llm_provider)


if __name__ == "__main__":
    rt = build_lab(create_mission=False)
    print(f"Loaded {len(LAB_BUNDLE)} packs:")
    for p in LAB_BUNDLE:
        print(f"  - {p.name} v{p.version}")
