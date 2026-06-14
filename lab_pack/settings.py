"""Settings for the lab pack.

All fields have defaults — the pack works with zero configuration.
"""

from __future__ import annotations

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
    research_worker_enabled: bool = Field(
        default=False,
        description=(
            "If True, the lab-local research worker (ADR-020) claims tasks "
            "routed research.deep_research and gathers sources through "
            "tool_gateway. Defaults False so no embedding, fixture, or test "
            "reaches the network by surprise; the server boot enables it — "
            "the live lab always runs the worker. Droppable: disabled, the "
            "capability-gap path takes over unchanged."
        ),
    )
    research_fetch_cap: int = Field(
        default=8,
        ge=1,
        description=(
            "Per-task cap on source fetches by the research worker "
            "(ADR-020). Seam-eligible — tuning research thoroughness is "
            "self-modification through the gate."
        ),
    )
    model_research_worker: str = Field(
        default="claude-opus-4-8",
        description="Model for the research_worker behavior (ADR-019/020).",
    )
    # ── code worker + repo sandbox (ADR-035, rung 2) ────────────────────────
    code_worker_enabled: bool = Field(
        default=False,
        description=(
            "If True, the lab-local code worker (ADR-035) claims tasks routed "
            "codebase.code_task, clones an allowlisted repo into the repo "
            "sandbox, runs a specified command (and, for a fix-task, applies a "
            "proposed diff and re-runs to prove it), and writes the captured "
            "output as attributed evidence. Defaults False so no fixture or "
            "embedding clones/runs by surprise; the server boot enables it — "
            "the live lab runs the worker. Droppable like the research worker: "
            "disabled, codebase.code_task is a dead lane again (capability gap)."
        ),
    )
    code_run_cap: int = Field(
        default=2,
        ge=1,
        le=8,
        description=(
            "Per-task cap on sandbox command runs by the code worker "
            "(ADR-035): a plain command is 1 run, a fix-task (command, apply "
            "diff, re-run) is 2. Bounds runaway re-runs. Seam-eligible."
        ),
    )
    sandbox_timeout_seconds: int = Field(
        default=300,
        ge=5,
        le=1800,
        description=(
            "Wall-clock timeout for ONE repo-sandbox command run (ADR-035). "
            "The run is killed on expiry and the outcome recorded as evidence. "
            "Seam-eligible — tuning the sandbox budget is self-modification."
        ),
    )
    model_code_worker: str = Field(
        default="claude-opus-4-8",
        description=(
            "Model for the code_worker behavior (ADR-019/035). Defaults to the "
            "deliberate plane (claude-opus-4-8) — reasoning over a code change "
            "and its test output is top-tier work per the operator's tiering. "
            "The diff-authoring stage (code_author, ADR-037) routes through "
            "this same setting — authoring a fix is top-tier reasoning too."
        ),
    )
    code_author_max_attempts: int = Field(
        default=2,
        ge=1,
        le=5,
        description=(
            "Bound on the code_worker's diff-authoring retry loop (ADR-037): "
            "the LLM authors a unified diff from the brief, the lab applies it "
            "in the sandbox and runs the proof command, and on failure the "
            "captured output is fed back for up to this many authoring attempts "
            "total. After the bound, the worker records an honest 'authored a "
            "diff but could not make it pass' evaluation and opens NO submit_pr "
            "— a fix must earn its PR by proving in the sandbox."
        ),
    )
    # ── self-dispatched code repair (ADR-036, the self-repair loop's planner) ─
    max_self_repair_branches: int = Field(
        default=3,
        ge=0,
        le=16,
        description=(
            "Guardrail (ADR-036): the maximum number of concurrent (non-"
            "decided, non-archived) SELF-PROPOSED code-repair branches the "
            "planner may have open at once. The planner self-dispatches a "
            "code-fix branch from the lab's OWN observed defects (routing "
            "misses, tagged code-defect findings) about its OWN allowlisted "
            "repo; this caps how many such branches can be in flight so self-"
            "repair cannot run wild. Deliberately NOT seam-eligible — a "
            "guardrail on self-modification stays in git, not in a gated "
            "decision the lab could raise on itself. 0 disables self-dispatch."
        ),
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
    mcp_reply_wait_seconds: int = Field(
        default=15,
        ge=1,
        le=60,
        description=(
            "RETIRED (ADR-034), kept as a no-op. send_chat is now "
            "commit-and-return-immediately (status=accepted + message event "
            "ids; the reply is read via get_branch), so there is no bounded "
            "reply wait left to tune — the recurring production timeouts the "
            "bound caused (evt_14234/16799: the mutation committed but the "
            "operator's call timed out) are gone with the wait. The field and "
            "its seam-whitelist entry (lab_pack/kernel.py) are kept in place "
            "rather than edited out, so no kernel change is required; nothing "
            "reads this value anymore."
        ),
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
    # Model routing (ADR-019): per-behavior model selection. Each field is
    # seam-whitelisted as setting.model.<behavior> (dots map to underscores
    # here), so rerouting a behavior is self-modification through the gate —
    # no restart. The resolution is stamped onto behavior.model, which the
    # runtime records natively on every llm.requested event. Keys come from
    # env vars only; LAB_LLM_MODEL still overrides the provider default.
    model_plan: str = Field(
        default="claude-opus-4-8",
        description="Model for the plan behavior (ADR-019).",
    )
    model_interpret: str = Field(
        default="claude-opus-4-8",
        description="Model for the interpret behavior (ADR-019).",
    )
    model_draft_writer: str = Field(
        default="claude-opus-4-8",
        description="Model for the draft_writer behavior (ADR-019).",
    )
    model_answer: str = Field(
        default="claude-sonnet-4-20250514",
        description="Model for the answer behavior (fast plane, ADR-019).",
    )
    model_default: str = Field(
        default="claude-sonnet-4-20250514",
        description=(
            "Model for any lab llm_behavior without its own routing entry, "
            "and the Anthropic provider default (ADR-019)."
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
        default=50.0,
        ge=0.0,
        description=(
            "Daily LLM cost cap in USD, UTC reset (ADR-015/019). Spend is "
            "rebuilt from the cost_usd activegraph stamps on llm.responded "
            "events (native per-model cost reporting — never a hardcoded "
            "price) — restart-proof. Blocked-by-cost attempts log like "
            "blocked-by-count. Seam-eligible, but every cap mechanism clamps "
            "to the kernel ABSOLUTE_DAILY_COST_CEILING_USD (ADR-019), which "
            "no seam or MCP call can move."
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
