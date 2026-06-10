"""activegraph.packs.lab — the activegraph-lab research agent pack.

One agent, one mission: grow activegraph.ai's evidence base. A layered pack
on the activegraph-packs core pack (ADR-003): three lab object types
(mission, branch, decision) over core's seven primitives.

Dependency declarations (packs-repo convention — documented, not enforced):
  requires        = ["core"]
  integrates_with = ["communication", "research", "codebase",
                     "tool_gateway", "memory_gateway"]
All integrations are optional; the pack degrades gracefully without them
(no tool_gateway → no crawl; no communication → no threads/answers).

Usage:
    from activegraph import Graph, Runtime
    from packs.core import pack as core_pack, CoreSettings
    from lab_pack import pack as lab_pack, LabSettings

    rt = Runtime(Graph(), llm_provider=provider)
    rt.load_pack(core_pack, settings=CoreSettings())
    rt.load_pack(lab_pack, settings=LabSettings())

Or compose everything at once: lab_pack.bundle.build_lab().
"""

from __future__ import annotations

from pathlib import Path

from activegraph.packs import Pack, load_prompts_from_dir

from .behaviors import BEHAVIORS, clear_lab_registry
from .object_types import OBJECT_TYPES, RELATION_TYPES
from .settings import LabSettings
from .tools import TOOLS

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# requires=["core"], integrates_with=["communication", "research", "codebase",
# "tool_gateway", "memory_gateway"] — see module docstring.
pack = Pack(
    name="lab",
    version="0.1.0",
    description=(
        "Self-hosted research agent: crawls its mission's site through "
        "tool_gateway, extracts claims as observations, proposes evidence-gap "
        "branches, dispatches work as core tasks (emergent coordination), "
        "interprets outcomes, and gates everything that publishes or "
        "self-modifies behind decision objects."
    ),
    object_types=tuple(OBJECT_TYPES),
    relation_types=tuple(RELATION_TYPES),
    behaviors=tuple(BEHAVIORS),
    tools=tuple(TOOLS),
    policies=(),
    prompts=load_prompts_from_dir(_PROMPTS_DIR) if _PROMPTS_DIR.exists() else (),
    settings_schema=LabSettings,
)

__all__ = ["pack", "LabSettings", "clear_lab_registry"]
