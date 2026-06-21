"""The daily heartbeat — a bounded, gated, killable standing behavior (ADR-044).

PLUMBING tier (like the watchdog): droppable. Disable the cadence seam and
the lab is reactive-only again, no deploy.

WHY THIS IS A DEPARTURE. The lab's core design is reactive: the log is the
agent, there is no orchestrator and no scheduler (ADR-001..008). The
heartbeat is the FIRST external clock that pokes the agent on a timer — a
deliberate architectural change, recorded honestly in ADR-044 with its
bounds.

HOW IT STAYS BOUNDED.
  * Wall-clock, not a loop. `maybe_heartbeat(rt)` is called from the same
    request/tick chokepoints as the stall watchdog (`check_stalls`). It does
    a wall-clock WINDOW check against the heartbeat events already in the log
    and fires AT MOST ONCE per cadence window. Stamping the window key into
    the event payload makes it idempotent across a mid-window restart — the
    log is the persistence, no side state (CONTRACT.md).
  * One step of fresh input, then react. A bare planner tick on an idle
    graph just re-proposes from stale findings ("cadence outruns evidence").
    So each tick advances ONE item of the operator's worklist FIRST
    (`heartbeat_worklist`) — bringing in new observations — then lets the
    EXISTING reactive planner react to them (run_until_idle). The heartbeat
    advances the backlog by one step; it never invents work from nothing.
  * Budget floored. Before any work, if today's LLM spend is at/above
    `heartbeat_budget_ceiling_usd` (default $15, well under the $50 daily cap
    and the $100 kernel ceiling), the tick records `heartbeat.skipped:budget`
    and does nothing. The heartbeat can never approach the daily cap on its
    own.
  * THE GATE IS UNCHANGED. Everything the heartbeat causes still lands as
    PROPOSALS in the operator inbox. The heartbeat NEVER approves, promotes,
    or submits a PR. It cannot self-approve anything. It only activates an
    existing branch (a reversible operator-tier verb, ADR-025) or queues a
    crawl_request the ingest behavior already reacts to — both of which feed
    the normal reactive pipeline whose every publish/self-modify/PR last mile
    is still a human decision.
  * Killable without a deploy. Set the `heartbeat_cadence` seam (or env) to
    "off" and the heartbeat stops instantly, no deploy. A global `pause_lab`
    (ADR-015) also stops it (it respects the pause like every other worker).
  * Audit. Every tick emits a `heartbeat.fired` or `heartbeat.skipped`
    marker event (the marker family, ADR-013/015/021) carrying the window,
    the reason, and what it advanced — the daily behavior is fully legible.

FAN-OUT WARNING (ADR-044 follow-up). A daily `recrawl` step exercises the
known fan-out amplification (~6-7x no-op behavior; one recrawl recently
generated ~13k events) every day, unattended. The DEFAULT worklist is
therefore the cheapest useful step (`advance_branch`), NOT a recrawl —
recrawl is opt-in. Trigger-debouncing / fan-out reduction should land before
`recrawl` is rotated into the unattended worklist, because the heartbeat
compounds that cost daily.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# Cadence sentinel. Anything not recognized is treated as "daily" — a typo in
# a seam body must never make the heartbeat fire FASTER.
CADENCE_OFF = "off"

# The marker events a tick may emit (both consume the cadence window). Listed
# so the idempotency scan and the audit surfaces agree on the taxonomy.
TICK_EVENTS = ("heartbeat.fired", "heartbeat.skipped")

# The operator-tunable worklist vocabulary. Each step brings in ONE unit of
# fresh input, then the reactive planner reacts. Ordered cheap → expensive.
KNOWN_STEPS = ("advance_branch", "research_direction", "recrawl")
_DEFAULT_WORKLIST = ("advance_branch",)


# ── settings resolution (seam-aware) ─────────────────────────────────────────

def _settings_and_graph(rt):
    from .settings import LabSettings
    return LabSettings(), rt.graph


def _cadence(graph, settings) -> str:
    from .seams import effective_setting
    val = str(effective_setting(graph, settings, "heartbeat_cadence")
              or "daily").strip().lower()
    return val or "daily"


def _worklist(graph, settings) -> tuple[str, ...]:
    from .seams import effective_setting
    raw = str(effective_setting(graph, settings, "heartbeat_worklist") or "")
    steps: list[str] = []
    for token in raw.split(","):
        name = token.strip().lower()
        if name in KNOWN_STEPS and name not in steps:
            steps.append(name)
    return tuple(steps) if steps else _DEFAULT_WORKLIST


def _budget_ceiling(graph, settings) -> float:
    from .seams import effective_setting
    try:
        return float(effective_setting(
            graph, settings, "heartbeat_budget_ceiling_usd"))
    except (TypeError, ValueError):
        return float(settings.heartbeat_budget_ceiling_usd)


# ── the cadence window ───────────────────────────────────────────────────────

def _window_key(cadence: str, now: datetime) -> Optional[str]:
    """The identifier of the cadence window `now` falls in, or None when the
    heartbeat is disabled. Stamped into every tick event so idempotency does
    not depend on event timestamps (and so a window rollover is testable)."""
    if cadence == CADENCE_OFF:
        return None
    # Everything else (including unknown values) is daily: one tick per UTC
    # calendar day.
    return "daily:" + now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _ticks(graph) -> list:
    return [e for e in graph.events if str(e.type) in TICK_EVENTS]


def _window_already_ticked(graph, window: str) -> bool:
    """A tick (fired OR skipped) whose payload window matches → the window is
    spent. Restart-safe: the window key rides in the durable event payload."""
    for e in _ticks(graph):
        if str((e.payload or {}).get("window") or "") == window:
            return True
    return False


def _next_cursor(graph, worklist_len: int) -> int:
    """Rotation position from the last heartbeat.fired event's worklist_index
    (rebuilt from the log — no side state). Starts at 0."""
    last_index = None
    for e in graph.events:
        if str(e.type) == "heartbeat.fired":
            idx = (e.payload or {}).get("worklist_index")
            if isinstance(idx, int):
                last_index = idx
    if last_index is None or worklist_len <= 0:
        return 0
    return (last_index + 1) % worklist_len


# ── object selection helpers (full-graph scans: the heartbeat runs OUTSIDE
#    behaviors, like check_stalls, so collection scans are safe) ───────────────

def _id_key(obj) -> tuple:
    """Sort branches/objects by their numeric id ('branch#62' → 62) so
    'oldest' is deterministic, with the raw id as a tiebreaker."""
    raw = str(obj.id)
    num = raw.rsplit("#", 1)[-1]
    try:
        return (int(num), raw)
    except ValueError:
        return (1 << 62, raw)


def _oldest_branch(graph, statuses: tuple[str, ...]):
    cands = [b for b in graph.objects(type="branch")
             if b.data.get("status") in statuses]
    return min(cands, key=_id_key) if cands else None


def _branches_with_operator_direction(graph) -> set[str]:
    """Branch ids that carry an operator-noted direction — an operator_direction
    (ADR-027 continuation) or an operator_note (ADR-028). These are the
    operator's queued research directions the heartbeat can pursue."""
    out: set[str] = set()
    for o in graph.objects(type="observation"):
        meta = o.data.get("metadata") or {}
        if meta.get("lab") in ("operator_direction", "operator_note"):
            bid = meta.get("lab_branch_id")
            if bid:
                out.add(str(bid))
    return out


# ── activation (the heartbeat's own provenance) ──────────────────────────────

def _activate_branch(graph, branch_id: str, status: str, reason: str) -> str:
    """Activate an existing branch on the heartbeat's authority — a reversible
    operator-tier move (ADR-025): record the rationale as an observation, reset
    the dispatch dedup so a fresh task is created, and patch status → active so
    the EXISTING `work` behavior dispatches. Nothing new dispatches here; the
    heartbeat NEVER creates or resolves a decision."""
    from .behaviors import reset_dispatch_dedup
    branch = graph.get_object(branch_id)
    obs = graph.add_object("observation", {
        "text": (f"Branch activated by the daily heartbeat (ADR-044) — "
                 f"from {status}. {reason}"),
        "confidence": 1.0,
        "category": "fact",
        "metadata": {"lab": "branch_activated", "lab_branch_id": branch_id,
                     "previous_status": status, "source": "heartbeat"},
    })
    graph.add_relation(branch_id, obs.id, "supported_by")
    reset_dispatch_dedup(branch_id)
    graph.patch_object(branch_id, {"status": "active"})
    return obs.id


# ── the worklist steps ───────────────────────────────────────────────────────
# Each returns an "advance record" dict {step, target, detail, refs} when it
# found work to advance, else None (no work — the rotation falls through).

def _step_advance_branch(graph) -> Optional[dict]:
    branch = _oldest_branch(graph, ("proposed", "scoped"))
    if branch is None:
        return None
    status = branch.data.get("status")
    obs_id = _activate_branch(
        graph, str(branch.id), status,
        "Advancing the proposal backlog by one branch; the planner reacts to "
        "the new task outcome.")
    return {"step": "advance_branch", "target": str(branch.id),
            "detail": f"activated branch {branch.id} (was {status})",
            "refs": {"rationale_observation": obs_id}}


def _step_research_direction(graph) -> Optional[dict]:
    directed = _branches_with_operator_direction(graph)
    if not directed:
        return None
    cands = [b for b in graph.objects(type="branch")
             if str(b.id) in directed
             and b.data.get("status") in ("proposed", "scoped", "decided")]
    if not cands:
        return None
    branch = min(cands, key=_id_key)
    status = branch.data.get("status")
    obs_id = _activate_branch(
        graph, str(branch.id), status,
        "Pursuing the next operator-noted research direction; the operator "
        "direction on record rides with the dispatched task.")
    return {"step": "research_direction", "target": str(branch.id),
            "detail": f"pursued operator direction on branch {branch.id}",
            "refs": {"rationale_observation": obs_id}}


def _step_recrawl(graph) -> Optional[dict]:
    # EXPENSIVE (fan-out): opt-in only, never in the default worklist.
    from .behaviors import reset_crawl_dedup
    mission = None
    for m in graph.objects(type="mission"):
        if (m.data.get("target_url") or "").strip():
            mission = m
            break
    if mission is None:
        return None
    target = (mission.data.get("target_url") or "").strip()
    reset_crawl_dedup(str(mission.id))
    req = graph.add_object("source", {
        "kind": "crawl_request",
        "content": target,
        "url": target,
        "channel": "lab",
        "metadata": {"mission_id": str(mission.id), "depth": 0,
                     "requested_by": "heartbeat"},
    })
    return {"step": "recrawl", "target": str(mission.id),
            "detail": f"queued a fresh crawl of {target}",
            "refs": {"crawl_request": str(req.id), "mission_id": str(mission.id)}}


_STEP_FNS = {
    "advance_branch": _step_advance_branch,
    "research_direction": _step_research_direction,
    "recrawl": _step_recrawl,
}


# ── the tick ─────────────────────────────────────────────────────────────────

def _emit(graph, event_type: str, payload: dict) -> Optional[str]:
    """Append a heartbeat marker event and return its id (single-threaded
    runtime — the freshly appended event is the log tail)."""
    from .behaviors import emit_lab_event
    emit_lab_event(graph, event_type, payload)
    try:
        return str(graph.events[-1].id)
    except (IndexError, AttributeError):
        return None


def maybe_heartbeat(rt, *, now: Optional[datetime] = None) -> dict:
    """Fire the daily heartbeat if its cadence window is due (ADR-044).

    Called from the same wall-clock chokepoints as `check_stalls` (the server
    feed poll, the overnight tick). At most ONE tick per cadence window,
    idempotent across a mid-window restart. Returns a result dict for the
    audit surfaces and the fixtures:
        {status: fired|skipped|noop, reason, step, target, event_id, window}
    """
    from .llm import daily_cost_today, lab_paused, sync_daily_budget

    settings, graph = _settings_and_graph(rt)
    now = now or datetime.now(timezone.utc)

    cadence = _cadence(graph, settings)
    window = _window_key(cadence, now)
    if window is None:                       # cadence == "off": the kill switch
        return {"status": "noop", "reason": "off", "window": None}

    # Respect the global pause (ADR-015) — the heartbeat is a worker too. No
    # event: the pause marker already records the operator's intent.
    if lab_paused():
        return {"status": "noop", "reason": "paused", "window": window}

    # Idempotency: one tick (fired OR skipped) per window. Restart-safe — the
    # window key lives in the durable event payload, not in process state.
    if _window_already_ticked(graph, window):
        return {"status": "noop", "reason": "already_ticked", "window": window}

    # BUDGET FLOOR. Refresh spend from the log, then gate. Over the ceiling →
    # record the skip and do NO work. The heartbeat cannot approach the cap.
    sync_daily_budget(rt)
    spent = float(daily_cost_today())
    ceiling = _budget_ceiling(graph, settings)
    if spent >= ceiling:
        eid = _emit(graph, "heartbeat.skipped", {
            "window": window, "cadence": cadence, "reason": "budget",
            "spent_usd": round(spent, 4), "ceiling_usd": round(ceiling, 4),
            "detail": (f"heartbeat skipped: budget (${spent:.2f} spent today "
                       f">= ${ceiling:.2f} heartbeat ceiling)")})
        return {"status": "skipped", "reason": "budget", "event_id": eid,
                "window": window, "spent_usd": spent, "ceiling_usd": ceiling}

    # Advance EXACTLY ONE worklist item: rotate from the cursor and take the
    # first step that has work. Then let the reactive planner react.
    worklist = _worklist(graph, settings)
    cursor = _next_cursor(graph, len(worklist))
    advanced: Optional[dict] = None
    chosen_index = cursor
    for offset in range(len(worklist)):
        idx = (cursor + offset) % len(worklist)
        rec = _STEP_FNS[worklist[idx]](graph)
        if rec is not None:
            advanced, chosen_index = rec, idx
            break

    if advanced is None:
        # Nothing to advance this window — still record a legible tick so the
        # window is consumed (no all-day retry) and the idle backlog is in the
        # log. Bounded and honest.
        eid = _emit(graph, "heartbeat.fired", {
            "window": window, "cadence": cadence, "step": "none",
            "worklist": list(worklist), "worklist_index": cursor,
            "detail": "heartbeat fired: no advanceable worklist item"})
        return {"status": "fired", "reason": "no_work", "step": "none",
                "event_id": eid, "window": window}

    eid = _emit(graph, "heartbeat.fired", {
        "window": window, "cadence": cadence, "step": advanced["step"],
        "worklist": list(worklist), "worklist_index": chosen_index,
        "target": advanced["target"], "refs": advanced.get("refs") or {},
        "detail": f"heartbeat fired: {advanced['detail']}"})

    # React: drive the existing reactive pipeline over the fresh input. The
    # planner reacts to new observations; every publish/self-modify/PR last
    # mile downstream is still a gated human decision (the gate is unchanged).
    rt.run_until_idle()

    return {"status": "fired", "reason": "advanced", "step": advanced["step"],
            "target": advanced["target"], "event_id": eid, "window": window}
