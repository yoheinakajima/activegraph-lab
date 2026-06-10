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
    # Operator controls (ADR-015): the daily cost ceiling.
    "daily_cost_cap_usd",
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
