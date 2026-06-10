#!/usr/bin/env python3
"""Public-safety audit (3a/3b, ADR-011): the whole event log, every object,
the feed JSON, captured boot output, and error paths must contain ZERO
traces of the secrets in the environment. DATABASE_URL is a credential too.

Sentinels are planted in the env, a full loop runs (chat, decision, fetch
failure, LLM anomaly, drafts), everything serializable is grepped.

Run:
    python scripts/test_public_safety.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

import os

SENTINELS = {
    "ANTHROPIC_API_KEY": "sk-ant-SENTINEL-vQ9zT3xK7w",
    "LAB_OPERATOR_TOKEN": "tok-SENTINEL-pL2mR8dN4c",
    "DATABASE_URL": "postgres://sentinel_user:pw-SENTINEL-aB5xY1@db.sentinel.internal:5432/lab",
}

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILURES.append(msg)


def main() -> int:
    saved = {k: os.environ.get(k) for k in SENTINELS}
    os.environ.update(SENTINELS)
    # Force mock so the sentinel "API key" is never sent anywhere.
    os.environ["LAB_LLM_PROVIDER"] = "mock"
    try:
        return _run()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("LAB_LLM_PROVIDER", None)


def _run() -> int:
    from lab_pack import clear_lab_registry, storage
    from lab_pack.bundle import build_lab
    from lab_pack.llm import (LabProviderWrapper, LabMockProvider,
                              reset_llm_session, _lab_prompt_bodies,
                              select_lab_provider)
    from lab_pack.settings import LabSettings
    from lab_pack.tools import (activate_branch_fn, approve_decision_fn,
                                complete_task_fn, send_branch_message_fn)
    from server.lab_server import _feed

    clear_lab_registry()
    reset_llm_session()
    boot_log = io.StringIO()

    pages = {"https://activegraph.ai": (
        "<p>ActiveGraph is an event-sourced agent runtime. Every mutation is "
        "appended to an event log before it changes graph state.</p>")}

    def fetch(url, **_kw):
        page = pages.get(url.rstrip("/"))
        if page is None:  # error path: a fetch failure observation
            return {"url": url, "status": 403, "content": "", "error": "HTTPError: 403 Forbidden"}
        return {"url": url, "status": 200, "content": page}

    with contextlib.redirect_stdout(boot_log):
        # The same calls the server boot makes — captured and audited.
        provider, info = select_lab_provider(settings=LabSettings())
        print(f"LLM: mode={info['mode']} provider={info['provider']}")
        print(f"boot: backend={storage.backend()}")
        # IMPORTANT: persist_to=None — the sentinel DATABASE_URL points
        # nowhere; backend() still reads it, which is exactly what we audit.
        rt = build_lab(
            # max_total=3: under ADR-014 the digest drafts once, so the
            # session makes fewer LLM calls — 3 still exhausts mid-run.
            llm_provider=LabProviderWrapper(LabMockProvider(), max_total=3,
                                            prompt_bodies=_lab_prompt_bodies()),
            lab_settings=LabSettings(drafts_dir=tempfile.mkdtemp()),
            fetch_handler=fetch)
        rt.run_until_idle()
        g = rt.graph
        # error paths: failed fetch, budget exhaustion, chat, full lifecycle
        from lab_pack.tools import request_crawl_fn
        mission = g.objects(type="mission")[0]
        request_crawl_fn(g, mission.id, "https://activegraph.ai/forbidden")
        rt.run_until_idle()
        branch = next(b for b in g.objects(type="branch") if b.data.get("status") == "proposed")
        activate_branch_fn(g, branch.id)
        rt.run_until_idle()
        tasks = [t for t in g.objects(type="task")
                 if (t.data.get("metadata") or {}).get("lab_branch_id") == branch.id]
        complete_task_fn(g, tasks[0].id, "verified.", True)
        rt.run_until_idle()
        send_branch_message_fn(g, branch.id, "status?")
        rt.run_until_idle()
        pend = [d for d in g.objects(type="decision") if d.data.get("status") == "pending"]
        if pend:
            approve_decision_fn(g, pend[0].id, True, "audit pass")
            rt.run_until_idle()

    # ── the corpus: every event payload, every object, feed JSON, boot log ──
    corpus = {
        "events": [{"type": str(e.type), "actor": str(e.actor),
                    "payload": e.payload} for e in g.events],
        "objects": [{"id": str(o.id), "type": str(o.type), "data": o.data}
                    for o in g.all_objects()],
        "feed": _feed(rt),
        "boot_log": boot_log.getvalue(),
    }
    blob = json.dumps(corpus, default=str)

    print(f"== sentinel audit over {len(g.events)} events, "
          f"{len(g.all_objects())} objects, the feed, and the boot log ==")
    for name, sentinel in SENTINELS.items():
        check(sentinel not in blob, f"{name} sentinel absent from the public corpus")
    # DATABASE_URL fragments count too (host, user, password)
    for frag in ("db.sentinel.internal", "sentinel_user", "pw-SENTINEL-aB5xY1"):
        check(frag not in blob, f"credential fragment '{frag}' absent")

    print("== exception hygiene (3b) ==")
    check("Traceback (most recent call last)" not in blob,
          "no tracebacks in any event/observation/feed payload")
    fetch_fails = [o for o in g.objects(type="observation")
                   if (o.data.get("metadata") or {}).get("lab") == "fetch_failure"]
    check(len(fetch_fails) >= 1, f"fetch-failure path exercised ({len(fetch_fails)})")
    budget = [o for o in g.objects(type="observation")
              if (o.data.get("metadata") or {}).get("lab") == "llm_budget"]
    check(len(budget) >= 1, f"budget-exhaustion path exercised ({len(budget)})")

    print(f"\ntest_public_safety: {'PASS' if not FAILURES else 'FAIL'} "
          f"({len(FAILURES)} failure(s))")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
