#!/usr/bin/env python3
"""The overnight run (Phase D): boot the real mission, let the full loop run,
queue everything that wants approval as pending decisions, approve NOTHING.

Loop: crawl → claims → proposed branches → activate the top 2 proposals
(with narrated reasoning) → work → gaps/completions → interpret →
draft_writer on whatever qualifies. Live LLM via ANTHROPIC_API_KEY (env only)
under the session budget; if the live site is unreachable from this
environment, the identical loop runs against a snapshot, records that the
crawl was synthetic, and every draft carries the limitation in its
provenance block.

Persists to data/lab.sqlite (the server resumes the same store), mirrors
drafts to drafts/. Run:
    python scripts/overnight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).parents[1]
sys.path.insert(0, str(REPO))

# A synthetic snapshot of activegraph.ai, used only when the live site is
# unreachable from this environment (D3). Content paraphrases the project's
# own published descriptions; the run records that it is a snapshot and every
# draft produced says so in its provenance block.
SNAPSHOT = {
    "https://activegraph.ai": """
<html><body>
<nav><a href="/docs">Docs</a> <a href="/packs">Packs</a></nav>
<h1>ActiveGraph</h1>
<p>ActiveGraph is an event-sourced agent runtime: every mutation is appended
to an event log before it changes graph state, so any agent decision can be
replayed and audited after the fact.</p>
<p>Behaviors fire automatically when matching objects appear in the graph;
there is no central orchestrator and no main loop to write.</p>
</body></html>""",
    "https://activegraph.ai/docs": """
<html><body>
<a href="/packs">Packs</a>
<p>The runtime supports deterministic forks anchored to committed events, and
replay rebuilds the graph projection without re-firing behaviors.</p>
<p>Every prompt an LLM behavior sends is assembled by the runtime from a
declared graph view, so the exact context of any model call is inspectable.</p>
</body></html>""",
    "https://activegraph.ai/packs": """
<html><body>
<a href="/docs">Docs</a>
<p>Packs compose through graph state, not function calls: a behavior in one
pack writes an object and behaviors in other packs react to it.</p>
<p>Tool calls are policy gated — the model proposes, the runtime authorizes,
executes, and records the result as a source object.</p>
</body></html>""",
}


def main() -> int:
    from lab_pack import clear_lab_registry
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (llm_usage, reset_llm_run_counters,
                              reset_llm_session, select_lab_provider)
    from lab_pack.settings import LabSettings
    from lab_pack.tools import activate_branch_fn, default_fetch_url
    from lab_pack.watchdog import check_stalls

    db = REPO / "data" / "lab.sqlite"
    db.parent.mkdir(exist_ok=True)
    if db.exists():
        print(f"[overnight] refusing to overwrite existing {db} — move it first")
        return 1

    settings = LabSettings(drafts_dir=str(REPO / "drafts"))
    provider, info = select_lab_provider(settings=settings)
    print(f"[overnight] LLM: mode={info['mode']} provider={info['provider']} "
          f"model={info.get('model')} | budget: "
          f"{settings.max_llm_calls_per_behavior_run}/behavior-run, "
          f"{settings.max_total_llm_calls_per_session}/session")

    # D3: probe the live site once; fall back to the snapshot if unreachable.
    probe = default_fetch_url("https://activegraph.ai")
    synthetic = probe.get("status") != 200
    if synthetic:
        print(f"[overnight] live site unreachable (status={probe.get('status')}, "
              f"{probe.get('error')}) — running the identical loop against the snapshot")

        def fetch(url, **_kw):
            page = SNAPSHOT.get(url.rstrip("/"))
            return ({"url": url, "status": 200, "content": page} if page else
                    {"url": url, "status": 404, "content": "", "error": "404 (snapshot)"})
    else:
        print("[overnight] live site reachable — crawling for real")
        fetch = None  # build_lab default = live fetcher

    clear_lab_registry()
    reset_llm_session()
    rt = build_lab(llm_provider=provider, lab_settings=settings,
                   fetch_handler=fetch, persist_to=str(db))
    g = rt.graph

    mission = g.objects(type="mission")[0]
    if synthetic:
        meta = dict(mission.data.get("metadata") or {})
        meta["crawl_mode"] = "synthetic"
        g.patch_object(mission.id, {"metadata": meta})
        g.add_object("observation", {
            "text": (f"The crawl this run was SYNTHETIC: the live site returned "
                     f"status {probe.get('status')} from this environment, so the "
                     "loop ran against a snapshot of activegraph.ai's published "
                     "descriptions. Site claims below rest on that snapshot."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "synthetic_crawl", "mission_id": mission.id},
        })

    print("[overnight] drain 1: crawl → claims → proposals + seeded findings → drafts")
    reset_llm_run_counters()
    rt.run_until_idle()
    check_stalls(rt)

    proposals = [b for b in g.objects(type="branch") if b.data.get("status") == "proposed"]
    print(f"[overnight] {len(proposals)} branch(es) proposed")

    # D1: activate the top 2 proposals. Selection is narrated, never scored:
    # take the first proposal per source page until we have two, so the two
    # activated branches test claims from different parts of the site.
    chosen, seen_urls = [], set()
    for b in proposals:
        obs_id = (b.data.get("metadata") or {}).get("claim_observation_id")
        obs = g.get_object(obs_id) if obs_id else None
        url = ((obs.data.get("metadata") or {}).get("url") if obs else None) or "?"
        if url not in seen_urls or len(proposals) <= 2:
            chosen.append(b)
            seen_urls.add(url)
        if len(chosen) == 2:
            break
    for b in (proposals[:2] if not chosen else chosen)[:2]:
        if b not in chosen:
            chosen.append(b)

    if chosen:
        reasoning = (
            f"Activated {len(chosen)} of {len(proposals)} proposals: "
            + "; ".join(f"'{b.data.get('title')}'" for b in chosen)
            + ". Chosen because they were the first proposals grounded in "
              "different pages of the site, so the night's work tests claims "
              "from distinct parts of the surface rather than re-testing one "
              "page twice. The rest stay proposed for the operator to triage."
        )
        g.add_object("observation", {
            "text": reasoning,
            "confidence": 0.9,
            "category": "decision",
            "metadata": {"lab": "activation_rationale", "mission_id": mission.id,
                         "branch_ids": [b.id for b in chosen]},
        })
        print(f"[overnight] activating: {[b.data.get('title')[:50] for b in chosen]}")
        for b in chosen:
            reset_llm_run_counters()
            activate_branch_fn(g, b.id)
            rt.run_until_idle()
            check_stalls(rt)

    print("[overnight] final drain + stall check")
    reset_llm_run_counters()
    rt.run_until_idle()
    check_stalls(rt)
    rt.save_state()

    usage = llm_usage()
    pending = [d for d in g.objects(type="decision") if d.data.get("status") == "pending"]
    drafts = [a for a in g.objects(type="artifact") if a.data.get("kind") == "blog_draft"]
    published = [a for a in drafts if a.data.get("status") == "published"]
    print(f"[overnight] done: {len(g.events)} events | {len(pending)} pending decisions "
          f"| {len(drafts)} drafts (published: {len(published)} — must be 0) "
          f"| llm calls: {usage['total']}/{settings.max_total_llm_calls_per_session}")
    if published:
        print("[overnight] GATE VIOLATION — drafts published without operator approval!")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
