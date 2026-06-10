"""Settings for the lab pack.

All fields have defaults — the pack works with zero configuration.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class LabSettings(BaseModel):
    """Configuration for the lab pack."""

    crawl_enabled: bool = Field(
        default=True,
        description="If False, ingest does not crawl on mission.created (fixtures drive sources manually).",
    )
    crawl_max_depth: int = Field(
        default=2,
        ge=0,
        le=2,
        description="Maximum link depth from target_url. CONTRACT cap: 2.",
    )
    crawl_page_cap: int = Field(
        default=30,
        ge=1,
        le=30,
        description="Maximum pages fetched per mission crawl. CONTRACT cap: 30.",
    )
    max_claims_per_page: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum claim observations ingest extracts from one page.",
    )
    progress_interval_seconds: int = Field(
        default=60,
        ge=1,
        description=(
            "Worker contract: a progress event at least this often, or the step "
            "is declared uninterruptible. Ingest emits one per page regardless."
        ),
    )
    max_open_branches: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Plan stops proposing new branches past this many non-archived branches.",
    )
    dispatch_gap_check: bool = Field(
        default=True,
        description=(
            "If True, work checks one event boundary after dispatch whether any "
            "pack reacted to the task, and records a capability-gap observation "
            "if not. A gap is evidence, not an error."
        ),
    )
    auto_approve_answers: bool = Field(
        default=True,
        description=(
            "If True, answer's comm_response_candidate is created status=approved "
            "so a channel adapter delivers it immediately. Answers are replies, "
            "not external writes — publishing stays gated regardless."
        ),
    )
    answer_channel: str = Field(
        default="lab",
        description="comm_message channel the answer behavior listens on.",
    )
    digest_min_findings: int = Field(
        default=3,
        ge=1,
        description=(
            "Editorial policy (ADR-014): findings accumulate as queued "
            "observations; when at least this many unpublished queued findings "
            "exist, the digest behavior requests ONE combined note-kind draft. "
            "Seam-eligible — tuning editorial policy is self-modification."
        ),
    )
    research_min_evidence: int = Field(
        default=3,
        ge=1,
        description=(
            "Editorial policy (ADR-014): a research/build draft requires a "
            "decided branch with at least this many linked evidence objects, "
            "or a synthesis of >=2 decided branches whose combined evidence "
            "meets this bar. Seam-eligible."
        ),
    )
    max_drafts_pending: int = Field(
        default=5,
        ge=1,
        description=(
            "Editorial policy (ADR-014): when the inbox holds this many "
            "pending publish decisions, automatic drafting idles and records "
            "an observation — the operator's attention is also a budget. "
            "Operator-requested drafts (via chat) bypass the cap. Seam-eligible."
        ),
    )
    drafts_dir: str = Field(
        default="drafts",
        description=(
            "Directory where blog_draft artifacts are mirrored as <slug>.md "
            "for easy reading. The graph copy is canonical; the file is a mirror."
        ),
    )
    model: Optional[str] = Field(
        default="claude-sonnet-4-20250514",
        description=(
            "Model for the lab's llm_behaviors when the Anthropic provider is "
            "active (key from ANTHROPIC_API_KEY env var ONLY — never the graph, "
            "never logs). Ignored for other providers; LAB_LLM_MODEL overrides."
        ),
    )
    max_llm_calls_per_behavior_run: int = Field(
        default=5,
        ge=1,
        description=(
            "Per-behavior LLM call cap within one run cycle (counters reset by "
            "reset_llm_run_counters(), called by the server/runner per drain)."
        ),
    )
    max_llm_calls_per_day: int = Field(
        default=200,
        ge=1,
        description=(
            "Daily LLM call cap, UTC reset. Counted from llm.requested events "
            "in the log (persisted by construction; restart-safe). On "
            "exhaustion: one observation, then idle until the UTC date turns."
        ),
    )
    daily_cost_cap_usd: float = Field(
        default=5.0,
        ge=0.0,
        description=(
            "Daily LLM cost ceiling in USD, UTC reset (ADR-015). Spend is "
            "rebuilt from the cost_usd activegraph stamps on llm.responded "
            "events — restart-proof. Blocked-by-cost attempts log like "
            "blocked-by-count. Seam-eligible: tuning the ceiling is "
            "self-modification through the gate."
        ),
    )
    max_total_llm_calls_per_session: int = Field(
        default=60,
        ge=1,
        description=(
            "Hard session-wide LLM call cap. Exhaustion records an observation "
            "and stops cleanly — behaviors receive inert outputs, never errors."
        ),
    )
