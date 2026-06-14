"""KERNEL manifest (ADR-012). This module is itself kernel.

The four-tier residency ladder puts the things that govern
self-modification permanently in git: the gate, auth, runtime wiring,
replay, storage selection, and the loaders. Graph-stored seams and code
may never name, import from, shadow, or monkeypatch anything listed here
— the loaders refuse such artifacts before any decision is even raised.

Changing this file is a kernel change: git review, never a graph artifact.
"""

from __future__ import annotations

import re
from typing import Optional

# Protected module paths and symbols. A graph artifact (seam body or code
# draft) that references any of these is refused outright.
KERNEL_MANIFEST: tuple[str, ...] = (
    # the manifest and the loaders themselves
    "lab_pack.kernel",
    "lab_pack.seams",
    "lab_pack.graph_code",
    # storage adapter — backend selection happens in exactly one place
    "lab_pack.storage",
    # the gate: nothing publishes or self-modifies without it
    "lab_pack.behaviors.gate",
    "_apply_decision",
    "_revert_unapproved_publish",
    # auth middleware + server runtime wiring
    "server.lab_server",
    "LAB_OPERATOR_TOKEN",
    # the runtime itself: event loop, replay machinery, persistence
    "activegraph.runtime",
    "activegraph.store",
    "activegraph.core",
    # process-level escape hatches
    "os.environ",
    "subprocess",
    "importlib",
    "sys.modules",
)

# Absolute daily LLM spend ceiling (ADR-019). This is KERNEL: not a setting,
# not seam-eligible, not MCP-modifiable. Every cap mechanism — the settings
# default, an approved setting.daily_cost_cap_usd seam, the MCP set_budget
# operator control (ADR-021) — clamps to this value at the enforcement
# point in the budget path. Raising it is a git change, never a graph one.
ABSOLUTE_DAILY_COST_CEILING_USD: float = 100.00

# Settings that MAY be overridden through seams (ADR-012). Everything not
# listed is kernel-adjacent: auth, gating, LLM call budgets, and loader
# behavior are not seams. The daily COST ceiling is deliberately
# seam-eligible (ADR-015) — tuning spend policy is self-modification through
# the gate, while the call-count backstops stay in git. This whitelist is
# kernel.
SEAM_ELIGIBLE_SETTINGS: frozenset[str] = frozenset({
    "crawl_max_depth",
    "crawl_page_cap",
    "max_claims_per_page",
    "max_open_branches",
    "progress_interval_seconds",
    # Editorial policy (ADR-014): tuning these IS self-modification.
    "digest_min_findings",
    "research_min_evidence",
    "max_drafts_pending",
    # Operator controls (ADR-015): the daily cost ceiling (clamped to the
    # ABSOLUTE_DAILY_COST_CEILING_USD kernel constant, ADR-019).
    "daily_cost_cap_usd",
    # Model routing (ADR-019): per-behavior model selection. Dots map to
    # underscores on the LabSettings field (model.plan → model_plan).
    "model.plan",
    "model.interpret",
    "model.draft_writer",
    "model.answer",
    "model.research_worker",
    "model.code_worker",
    "model.default",
    # Research worker (ADR-020): per-task source-fetch cap.
    "research_fetch_cap",
    # Code worker + repo sandbox (ADR-035): per-task run cap and per-run
    # wall-clock budget — tuning sandbox thoroughness/spend is
    # self-modification through the gate, while the secret-isolation and
    # allowlist invariants stay in code (repo_sandbox is plumbing, not a seam).
    "code_run_cap",
    "sandbox_timeout_seconds",
    # MCP surface (ADR-016): send_chat's bounded reply wait — client-facing
    # latency policy, not auth, gating, or budget.
    "mcp_reply_wait_seconds",
})

# Word-ish boundary match so "lab_pack.kernel" hits imports, attribute
# chains, and string references, but "kernels_of_corn" does not.
_PATTERNS = [
    (entry, re.compile(r"(?<![\w.])" + re.escape(entry) + r"(?![\w])"))
    for entry in KERNEL_MANIFEST
]


def kernel_reference(text: str) -> Optional[str]:
    """Return the first KERNEL_MANIFEST entry referenced in `text`, else None.

    Used by the seam loader and the graph-code static checks. String-level on
    purpose: it also catches getattr-style and monkeypatch-by-name tricks
    that an AST import scan alone would miss.
    """
    for entry, pat in _PATTERNS:
        if pat.search(text or ""):
            return entry
    return None
