"""Lab pack tools — v0.1.

Plain `*_fn` implementations (callable from fixtures, the server, and tests —
the @tool wrapper is declarative and not directly callable, per the builder
report) plus @tool wrappers registered with the pack.

The web fetcher is registered as a tool_gateway local capability, never called
directly by behaviors: ingest proposes capability_calls and the gateway
executes them (CONTRACT: all fetches go through tool_gateway).
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from activegraph.packs import tool

from .behaviors import _THREAD_TO_BRANCH

_MAX_FETCH_CHARS = 200_000


_FETCH_UA = (
    "Mozilla/5.0 (compatible; activegraph-lab/0.1; "
    "+https://github.com/yoheinakajima/activegraph-lab)"
)


def default_fetch_url(url: str, **_kwargs) -> dict:
    """Default live web fetcher: real User-Agent, 20s timeout, 2 retries with
    backoff (1s, 2s). Never raises — a failed page returns {status, error} and
    ingest records it as a fetch-failure observation (a 403 is evidence)."""
    import time

    req = urllib.request.Request(url, headers={
        "User-Agent": _FETCH_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    })
    last: dict = {"url": url, "status": 0, "content": "", "error": "not attempted"}
    for attempt in range(3):  # initial try + 2 retries
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read(_MAX_FETCH_CHARS).decode("utf-8", errors="replace")
                return {"url": url, "status": resp.status, "content": body}
        except urllib.error.HTTPError as exc:
            last = {"url": url, "status": exc.code, "content": "",
                    "error": f"HTTPError: {exc.code} {exc.reason}"}
            if exc.code in (401, 403, 404, 410):
                break  # retrying won't change a definitive answer
        except Exception as exc:
            last = {"url": url, "status": 0, "content": "",
                    "error": f"{type(exc).__name__}: {exc}"}
        if attempt < 2:
            time.sleep(1 * (attempt + 1))
    return last


def register_web_fetch(handler=None, *, overwrite: bool = False) -> bool:
    """Register the 'web.fetch_url' capability with tool_gateway's local registry.

    Idempotent: an existing handler (e.g. a fixture's canned-page handler) is
    kept unless overwrite=True. Returns True if a handler was registered.

    OPEN (docs/ARCHITECTURE.md): whether fetch_url belongs upstream in
    tool_gateway. For now it lives here and is proposed upstream as an
    artifact, never as a direct edit (ADR-005).
    """
    try:
        from packs.tool_gateway.tools import _LOCAL_REGISTRY, register_local_capability
    except Exception:
        return False  # tool_gateway not installed — ingest will surface this as friction.
    if not overwrite and "web.fetch_url" in _LOCAL_REGISTRY:
        return False
    register_local_capability("web", "fetch_url", handler or default_fetch_url)
    return True


# ---------------------------------------------------------------- graph helpers


def create_mission_fn(graph, title: str, statement: str = "", target_url: str = ""):
    """Create the lab's mission. mission.created triggers ingest's crawl."""
    return graph.add_object("mission", {
        "title": title,
        "statement": statement,
        "target_url": target_url,
        "status": "active",
        "metadata": {},
    })


def create_branch_fn(
    graph,
    mission_id: str,
    title: str,
    intent: str = "",
    status: str = "proposed",
    authority: str = "gated",
    parent_branch_id: Optional[str] = None,
    fork_event_id: Optional[str] = None,
):
    """Create a branch under a mission (e.g. the read_the_website seed branch)."""
    branch = graph.add_object("branch", {
        "title": title,
        "intent": intent,
        "status": status,
        "authority": authority,
        "parent_branch_id": parent_branch_id,
        "fork_event_id": fork_event_id,
        "mission_id": mission_id,
        "metadata": {},
    })
    graph.add_relation(mission_id, branch.id, "has_branch")
    if parent_branch_id:
        graph.add_relation(branch.id, parent_branch_id, "forked_from")
    return branch


def activate_branch_fn(graph, branch_id: str):
    """Set a branch active — work dispatches it at the next event boundary."""
    return graph.patch_object(branch_id, {"status": "active"})


def link_thread_to_branch_fn(graph, thread_id: str, branch_id: str):
    """Create the discusses relation (thread = branch, ADR-004) + cache it."""
    _THREAD_TO_BRANCH[thread_id] = branch_id
    return graph.add_relation(thread_id, branch_id, "discusses")


def ensure_branch_thread_fn(graph, branch_id: str,
                            thread_id: Optional[str] = None) -> tuple[str, bool]:
    """Return (thread_id, created). Creates the comm_thread on first use
    (requires the communication pack). The discusses relation is NOT written
    here — it is post-commit upkeep relative to the message append (ADR-023),
    so callers write it after the message lands (or compose via
    send_branch_message_fn)."""
    if thread_id is not None:
        return thread_id, False
    thread = graph.add_object("comm_thread", {
        "channel": "lab",
        "subject": f"branch:{branch_id}",
        "status": "open",
        "created_at": "",
        "metadata": {"lab_branch_id": branch_id},
    })
    return thread.id, True


def append_branch_message_fn(
    graph,
    branch_id: str,
    content: str,
    user_ref: str = "owner",
    thread_id: str = "",
    source: Optional[str] = None,
):
    """THE message append — in any chat path this is the one step whose
    failure may fail the request (ADR-023). The message carries
    metadata.lab_branch_id so the answer behavior's view anchors on the
    branch. `source` tags metadata.source (ADR-016: operator_via_mcp marks
    chats the operator's assistant sent on their behalf)."""
    meta = {"lab_branch_id": branch_id, "thread_id_hint": thread_id}
    if source:
        meta["source"] = source
    return graph.add_object("comm_message", {
        "channel": "lab",
        "sender_ref": user_ref,
        "content": content,
        "direction": "inbound",
        "thread_id": thread_id,
        "metadata": meta,
    })


def send_branch_message_fn(
    graph,
    branch_id: str,
    content: str,
    user_ref: str = "owner",
    thread_id: Optional[str] = None,
    source: Optional[str] = None,
):
    """Post a user message into a branch's thread (channel='lab').

    Composes ensure_branch_thread_fn + append_branch_message_fn and links a
    new thread to the branch via discusses. Fixtures and tests call this
    composite; the server chat path composes the same primitives with each
    post-commit step individually guarded (ADR-023). Returns
    (thread_id, comm_message).
    """
    thread_id, created = ensure_branch_thread_fn(graph, branch_id, thread_id)
    if created:
        link_thread_to_branch_fn(graph, thread_id, branch_id)
    msg = append_branch_message_fn(graph, branch_id, content,
                                   user_ref=user_ref, thread_id=thread_id,
                                   source=source)
    return thread_id, msg


def approve_decision_fn(graph, decision_id: str, approved: bool, rationale: str = ""):
    """Resolve a pending decision. gate applies the outcome at the next boundary."""
    decision = graph.get_object(decision_id)
    patch: dict = {"status": "approved" if approved else "rejected"}
    if rationale and decision is not None:
        existing = decision.data.get("rationale") or ""
        patch["rationale"] = (existing + f"\nResolution: {rationale}").strip()
    return graph.patch_object(decision_id, patch)


def complete_task_fn(graph, task_id: str, result_summary: str, success: bool = True):
    """Mark a dispatched task done/failed (a worker pack — or a fixture — calls
    this). work turns the patch into a task_outcome evaluation for interpret."""
    task = graph.get_object(task_id)
    meta = dict((task.data.get("metadata") if task else {}) or {})
    meta["result_summary"] = result_summary
    return graph.patch_object(task_id, {
        "status": "done" if success else "rejected",
        "metadata": meta,
    })


def request_crawl_fn(graph, mission_id: str, url: str, depth: int = 0):
    """Ask ingest to fetch one more URL (a crawl_request source)."""
    return graph.add_object("source", {
        "kind": "crawl_request",
        "content": url,
        "url": url,
        "channel": "lab",
        "metadata": {"mission_id": mission_id, "depth": depth},
    })


# ---------------------------------------------------------------- @tool wrappers


@tool(name="create_mission", description="Create the lab mission; starts the site crawl.")
def create_mission(graph, title: str, statement: str = "", target_url: str = ""):
    return create_mission_fn(graph, title, statement, target_url)


@tool(name="create_branch", description="Create a branch of inquiry under the mission.")
def create_branch(graph, mission_id: str, title: str, intent: str = ""):
    return create_branch_fn(graph, mission_id, title, intent)


@tool(name="activate_branch", description="Activate a branch so work dispatches it.")
def activate_branch(graph, branch_id: str):
    return activate_branch_fn(graph, branch_id)


@tool(name="send_branch_message", description="Post a user message into a branch's thread.")
def send_branch_message(graph, branch_id: str, content: str, user_ref: str = "owner"):
    return send_branch_message_fn(graph, branch_id, content, user_ref)


@tool(name="approve_decision", description="Approve or reject a pending lab decision.")
def approve_decision(graph, decision_id: str, approved: bool, rationale: str = ""):
    return approve_decision_fn(graph, decision_id, approved, rationale)


TOOLS = [create_mission, create_branch, activate_branch, send_branch_message, approve_decision]
