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


if __name__ == "__main__":
    rt = build_lab(create_mission=False)
    print(f"Loaded {len(LAB_BUNDLE)} packs:")
    for p in LAB_BUNDLE:
        print(f"  - {p.name} v{p.version}")
