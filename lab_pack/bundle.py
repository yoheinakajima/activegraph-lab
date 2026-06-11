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
        "key": "postgres_store_immortal_connection",
        "text": (
            "Finding (upstream, activegraph core): PostgresEventStore opened "
            "from a URL holds a single boot-lifetime connection and assumes "
            "it is immortal; serverless Postgres guarantees the opposite — "
            "Neon suspends idle compute and terminates every connection with "
            "it. Production signature, twice with identical shape: the first "
            "write after an idle suspend failed AdminShutdown ('terminating "
            "connection due to administrator command'), then every "
            "subsequent write failed OperationalError ('the connection is "
            "closed') until a process restart. ADR-023 surfaced both "
            "structurally; nothing committed in between. The lab works "
            "around it in its storage adapter (the one backend-aware module, "
            "ADR-009): connection-class failures re-establish the connection "
            "and retry the operation exactly once, recorded on the "
            "diagnostics ring buffer as store_reconnected. Proposed upstream "
            "change: reconnect-with-bounded-retry belongs in the store "
            "itself — a store that owns its connection's lifecycle should "
            "own its death too. Constraint errors (UniqueViolation) must "
            "stay non-retried, which also keeps a retried append safe "
            "against double-commit via UNIQUE(id, run_id)."
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
        f = graph.add_object("observation", {
            "text": finding["text"],
            "confidence": 0.9,
            "category": "fact",
            "metadata": {"lab": "finding", "finding": True,
                         "finding_key": finding["key"],
                         "lab_branch_id": branch_id, "mission_id": mission_id,
                         "evidence_refs": []},
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
    rt.load_pack(tool_gateway_pack, settings=ToolGatewaySettings())
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
