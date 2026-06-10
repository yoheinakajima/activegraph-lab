"""Operational watchdog (A5): the overnight run must not hang silently.

Two layers:

1. Bounded externals — every call that leaves the process is already
   time-limited (fetches: 20s timeout × ≤3 attempts; LLM calls:
   timeout_seconds=60 per the llm_behavior declarations), so a
   run_until_idle drain always returns. The single-threaded runtime cannot
   be preempted mid-handler; the watchdog therefore operates at event
   boundaries, which is also where steering lands (CONTRACT.md).

2. check_stalls(rt) — called between run cycles (the overnight runner every
   tick, the server before each request): any lab work item that has emitted
   no progress event for `stall_seconds` gets a stall observation recorded
   and is released (task → blocked). Runs outside behaviors, so full graph
   scans are safe here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

STALL_SECONDS = 120


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _last_progress_for(graph, object_id: str) -> Optional[datetime]:
    """Latest event that touched this object (created or patched)."""
    latest = None
    for e in graph.events:
        touched = (
            e.payload.get("target") == object_id
            or (e.payload.get("object") or {}).get("id") == object_id
            or e.payload.get("id") == object_id
        )
        if touched:
            t = _parse_ts(getattr(e, "timestamp", None))
            if t and (latest is None or t > latest):
                latest = t
    return latest


def check_stalls(rt, stall_seconds: int = STALL_SECONDS) -> list[str]:
    """Record a stall observation and release any lab task whose last
    progress event is older than `stall_seconds`. Idempotent per task.
    Returns the released task ids."""
    g = rt.graph
    now = datetime.now(timezone.utc)
    already_stalled = {
        (o.data.get("metadata") or {}).get("task_id")
        for o in g.objects(type="observation")
        if (o.data.get("metadata") or {}).get("lab") == "stall"
    }
    released: list[str] = []
    for task in g.objects(type="task"):
        meta = task.data.get("metadata") or {}
        if not meta.get("lab_branch_id"):
            continue  # not lab work
        if task.data.get("status") != "active":
            continue  # done/blocked/rejected items are not in flight
        if task.id in already_stalled:
            continue
        last = _last_progress_for(g, task.id)
        if last is None:
            continue
        idle = (now - last).total_seconds()
        if idle < stall_seconds:
            continue
        contract = meta.get("progress_contract") or {}
        if contract.get("uninterruptible"):
            continue  # declared uninterruptible — the contract's escape hatch
        g.add_object("observation", {
            "text": (f"Stall: task '{task.data.get('title')}' emitted no progress "
                     f"event for {int(idle)}s (contract: every "
                     f"{contract.get('interval_seconds', 60)}s). Releasing the "
                     "work item (status=blocked)."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "stall", "task_id": task.id,
                         "lab_branch_id": meta.get("lab_branch_id"),
                         "idle_seconds": int(idle)},
        })
        g.patch_object(task.id, {"status": "blocked"})
        released.append(task.id)
    if released:
        rt.run_until_idle()
    return released
