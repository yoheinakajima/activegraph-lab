#!/usr/bin/env python3
"""Morning summary from the graph (D4). Read-only: replays data/lab.sqlite.

Run:
    python scripts/overnight_report.py
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))


def main() -> int:
    from activegraph import Runtime
    from lab_pack.settings import LabSettings

    db = os.environ.get("ACTIVEGRAPH_DB", str(REPO / "data" / "lab.sqlite"))
    if not Path(db).exists():
        print(f"No store at {db} — run: python scripts/overnight.py")
        return 1
    rt = Runtime.load(db)
    g = rt.graph
    settings = LabSettings()

    print("=" * 66)
    print("activegraph-lab — morning report")
    print("=" * 66)

    missions = g.objects(type="mission")
    if missions:
        m = missions[0]
        crawl = (m.data.get("metadata") or {}).get("crawl") or {}
        mode = (m.data.get("metadata") or {}).get("crawl_mode", "live")
        print(f"\nMission: {m.data.get('title')}")
        print(f"  target: {m.data.get('target_url')}  crawl: {mode}, "
              f"{crawl.get('fetched', 0)}/{crawl.get('page_cap', '?')} pages")

    print(f"\nEvents emitted: {len(g.events)}")

    branches = g.objects(type="branch")
    by_status = Counter(b.data.get("status") for b in branches)
    print(f"\nBranches ({len(branches)}): "
          + ", ".join(f"{s}: {n}" for s, n in sorted(by_status.items())))
    for b in branches:
        print(f"  [{b.data.get('status'):>12}] {b.data.get('title')[:58]}")

    obs = g.objects(type="observation")
    lab_kinds = Counter((o.data.get("metadata") or {}).get("lab") or "core/other"
                        for o in obs)
    print(f"\nObservations ({len(obs)}):")
    for kind, n in sorted(lab_kinds.items(), key=lambda x: -x[1]):
        print(f"  {kind}: {n}")

    drafts = [a for a in g.objects(type="artifact") if a.data.get("kind") == "blog_draft"]
    others = [a for a in g.objects(type="artifact") if a.data.get("kind") != "blog_draft"]
    print(f"\nArtifacts drafted: {len(drafts)} blog draft(s), {len(others)} other")
    for a in drafts:
        meta = a.data.get("metadata") or {}
        print(f"  [{a.data.get('status'):>9}] {a.data.get('title')[:50]} → drafts/{meta.get('slug')}.md")

    decisions = g.objects(type="decision")
    pending = [d for d in decisions if d.data.get("status") == "pending"]
    print(f"\nPending decisions — your inbox ({len(pending)} of {len(decisions)} total):")
    for d in pending:
        print(f"  [{d.data.get('kind'):>8}] {d.id}: "
              f"{(d.data.get('rationale') or '')[:70]}")
    if not pending:
        print("  (none — nothing awaits approval)")

    llm_calls = sum(1 for e in g.events if str(e.type) == "llm.requested")
    print(f"\nLLM calls used: {llm_calls} "
          f"(session budget: {settings.max_total_llm_calls_per_session})")

    stalls = lab_kinds.get("stall", 0)
    fails = {k: lab_kinds.get(k, 0)
             for k in ("fetch_failure", "llm_parse_failure", "llm_call_failure",
                       "llm_budget", "gate_violation")}
    print(f"\nStalls: {stalls} | failures: "
          + ", ".join(f"{k}: {n}" for k, n in fails.items()))

    published_unapproved = [
        a.id for a in drafts if a.data.get("status") == "published"
        and not any(d.data.get("subject_ref") == a.id and d.data.get("status") == "approved"
                    for d in decisions)
    ]
    print(f"\nGate check: "
          + ("VIOLATION — published without approval: " + str(published_unapproved)
             if published_unapproved else
             "clean — nothing published or self-modified without an approved decision"))

    print("\nOpen the feed:")
    print("  python server/lab_server.py")
    print("  open http://localhost:7799/")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
