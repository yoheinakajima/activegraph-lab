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
    model: Optional[str] = Field(
        default=None,
        description=(
            "LLM model name override for the lab's llm_behaviors. None resolves "
            "to the active provider's default_model at call time."
        ),
    )
