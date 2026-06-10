"""Lab pack object and relation types — v0.1.

Exactly three lab types (CONTRACT.md, ADR-003): mission, branch, decision.
Everything else the lab touches is a core primitive: artifact for outputs,
observation + evaluation for evidence, task for work dispatch, source for
ingested pages. Adding a fourth lab type requires a gated decision object
AND an ADR.

Relation convention: relations are written per the real
``Graph.add_relation(source_id, target_id, type)`` signature so runtime
view traversal works (see docs/ARCHITECTURE.md — the packs repo itself is
split on this).
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from activegraph.packs import ObjectType, RelationType


class Mission(BaseModel):
    """The lab's single standing purpose. One mission, created at bootstrap."""

    title: str = Field(description="Short mission title.")
    statement: str = Field(
        default="",
        description="One-paragraph statement of what the mission is and why.",
    )
    target_url: str = Field(
        default="",
        description="The site the mission grows evidence about (e.g. https://activegraph.ai).",
    )
    status: Literal["active", "paused", "completed", "archived"] = Field(
        default="active",
        description="Mission lifecycle status.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Crawl progress counters and other projection-friendly state.",
    )


class Branch(BaseModel):
    """A line of inquiry under the mission. A branch is also a thread (ADR-004)
    and, at fork time, a runtime fork anchored to a committed event."""

    title: str = Field(description="Short branch title.")
    intent: str = Field(
        default="",
        description="Free-text statement of what this branch is trying to find out or produce.",
    )
    status: Literal[
        "proposed", "scoped", "active", "paused", "interpreting", "decided", "archived"
    ] = Field(
        default="proposed",
        description="Branch lifecycle status. 'paused' added by ADR-007.",
    )
    parent_branch_id: Optional[str] = Field(
        default=None,
        description="ID of the branch this one forked from, if any.",
    )
    fork_event_id: Optional[str] = Field(
        default=None,
        description="Committed event the fork anchors to. Forks anchor to committed events only.",
    )
    authority: Literal["auto", "gated"] = Field(
        default="gated",
        description="One bit per capability: auto-run or gate on an approved decision.",
    )
    mission_id: Optional[str] = Field(
        default=None,
        description="Denormalized mission reference (the has_branch relation is canonical).",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Narrated planning reasoning, steering flags, decision refs.",
    )


class Decision(BaseModel):
    """A gate record. Nothing publishes or self-modifies without one approved."""

    subject_ref: str = Field(
        description="ID of the object this decision governs (branch, artifact, ...)."
    )
    kind: Literal["promote", "publish", "schema_change", "dependency_pin", "other"] = Field(
        default="other",
        description="What category of gate this is.",
    )
    status: Literal["pending", "approved", "rejected"] = Field(
        default="pending",
        description="Gate outcome. Mutated only by the owner (UI/API/steering message).",
    )
    rationale: str = Field(
        default="",
        description="Why this decision is being requested / why it was resolved this way.",
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description="IDs of observation/evaluation/task objects supporting the request.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


OBJECT_TYPES = [
    ObjectType(
        name="mission",
        schema=Mission,
        description="The lab's single standing purpose; the root every branch hangs off.",
    ),
    ObjectType(
        name="branch",
        schema=Branch,
        description="A line of inquiry under the mission; also a conversation thread and a fork anchor.",
    ),
    ObjectType(
        name="decision",
        schema=Decision,
        description="A gate record; publishing and self-modification always require one approved.",
    ),
]


RELATION_TYPES = [
    RelationType(
        name="has_branch",
        source_types=("mission",),
        target_types=("branch",),
        description="A mission has a branch of inquiry.",
    ),
    RelationType(
        name="forked_from",
        source_types=("branch",),
        target_types=("branch",),
        description="A branch forked from another branch at a committed event.",
    ),
    RelationType(
        name="produced",
        source_types=("branch",),
        target_types=("artifact",),
        description="A branch produced a core artifact.",
    ),
    RelationType(
        name="supported_by",
        source_types=("branch",),
        target_types=("observation", "evaluation"),
        description="A branch is supported by core evidence (observation or evaluation).",
    ),
    RelationType(
        name="dispatched",
        source_types=("branch",),
        target_types=("task",),
        description="A branch dispatched a core task for worker packs to react to.",
    ),
    RelationType(
        name="discusses",
        source_types=("comm_thread",),
        target_types=("branch",),
        description=(
            "A communication thread discusses a branch (ADR-004: thread = branch). "
            "Only created when the communication pack is loaded."
        ),
    ),
]
