"""Lab pack behaviors — v0.1.

Six small reactive behaviors, no orchestrator (docs/ARCHITECTURE.md):

  ingest    — mission.created / crawl_request source → capability_calls through
              tool_gateway → sources → claim observations. Depth/page capped,
              one progress event (mission patch) per page.
  plan      — llm_behavior. site_claim observation → proposed branch with
              narrated reasoning in the event payload.
  work      — branch activated → core task with routing tags; one event
              boundary later, capability-gap check (a gap is evidence, not an
              error); task done/failed → evaluation marker for interpret.
  interpret — llm_behavior. task-outcome evaluation → summary observation +
              pending promote decision (or follow-up branch).
  gate      — pending decision → approval-request event; approved/rejected →
              outcome applied. Nothing publishes without an approved decision.
  answer    — llm_behavior. inbound comm_message on the lab channel →
              event-horizon-stamped reply from graph state; steering messages
              also write the corresponding object mutation.

Coordination is emergent: the lab never calls domain packs — it writes core
tasks and reacts to what appears in the graph (ADR-006). All fetches go
through tool_gateway (CONTRACT.md).

Registries (convenience caches + dedup, never the source of truth; the
re-entrancy footgun in the builder report makes graph scans unsafe inside
behaviors). Call clear_lab_registry() between fixtures, and rebuild on
resume — replay does not re-fire behaviors:

  _CRAWLS            mission_id → {visited, fetched, queued}
  _CALLS             capability_call_id → {url, depth, mission_id}
  _WEB_PROVIDER_ID   the lab's capability_provider object id
  _PLANNED_OBS       observation ids plan already considered
  _BRANCH_COUNT      non-archived branch count (max_open_branches cap)
  _DISPATCHED        branch ids work already dispatched
  _GAP_CHECKED       task ids the gap check already ran for
  _EVALUATED         task ids already marked with an outcome evaluation
  _PENDING_BY_SUBJECT subject_ref → pending decision id
  _PENDING_BY_BRANCH branch id → pending decision ids (chat approve/reject
                     keys by the thread's branch; subject_ref alone missed
                     publish/self_modify decisions — ADR-025)
  _APPLIED_DECISIONS decision ids whose outcome gate already applied
  _APPROVED_PUBLISH  subject_refs with an approved publish decision
  _THREAD_TO_BRANCH  comm_thread id → branch id (discusses cache)
  _OPERATOR_DIRECTIONS branch id → operator continuation directions, oldest
                     first (ADR-027: a rejected promote's resolution_rationale
                     is teaching, not burial — dispatch reads the latest)
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from activegraph.packs import behavior, llm_behavior, load_prompts_from_dir

from .llm import (
    AnswerReply,
    BlogDraft,
    InterpretSummary,
    PlanProposal,
    SeamProposal,
    consume_llm_anomalies,
    is_inert,
    llm_usage,
)
from .seams import (
    charter_file_default,
    clear_seam_cache,
    composed_description,
    effective_setting,
    seam_versions_stamp,
)
from .settings import LabSettings

_PROMPTS = {p.name: p.body for p in load_prompts_from_dir(Path(__file__).parent / "prompts")}


def _file_default_description(name: str) -> str:
    """A behavior's file-default context: its prompt body, plus the verbatim
    CHARTER v1 block for the charter behaviors (ADR-018). Approved seams
    recompose this through lab_pack/seams.py at hot-load and boot."""
    return composed_description(name, _PROMPTS[name], 1, charter_file_default())


# The pack loader registers FRESH canonical-named copies of these behaviors
# (lab.plan, lab.answer, …) on every load_pack — deliberately, so frozen
# pack contents stay untouched. Anything that mutates live behavior state
# after load (prompt/charter seam hot-loads, model routing) must therefore
# mutate the RUNTIME'S copies, not just the module originals; mutating only
# the originals silently affects future loads and nothing else (discovered
# while wiring ADR-019 model routing). bind_live_behaviors is called
# wherever a runtime is built or resumed.
_LIVE_BEHAVIORS: list = []


def bind_live_behaviors(rt) -> int:
    """Capture the runtime's registered copies of the lab behaviors so seam
    hot-loads and model routing reach the live registration. Replaces any
    previous binding (one runtime per process outside fixtures)."""
    del _LIVE_BEHAVIORS[:]
    for b in BEHAVIORS:
        try:
            _LIVE_BEHAVIORS.append(rt.get_behavior(f"lab.{b.name}"))
        except Exception:
            pass
    return len(_LIVE_BEHAVIORS)


def behaviors_named(name: str) -> list:
    """Every behavior object answering to `name`: the module original plus
    the bound runtime copy (canonical lab.<name>), when one exists."""
    out = [b for b in BEHAVIORS if getattr(b, "name", "") == name]
    out += [b for b in _LIVE_BEHAVIORS
            if getattr(b, "name", "") == f"lab.{name}"]
    return out

# ---------------------------------------------------------------- registries

_CRAWLS: dict[str, dict] = {}
_CALLS: dict[str, dict] = {}
_WEB_PROVIDER_ID: dict[str, str] = {}
_PLANNED_OBS: set[str] = set()
_BRANCH_COUNT: dict[str, int] = {"open": 0}
_DISPATCHED: set[str] = set()
_GAP_CHECKED: set[str] = set()
_EVALUATED: set[str] = set()
_PENDING_BY_SUBJECT: dict[str, str] = {}
_PENDING_BY_BRANCH: dict[str, list[str]] = {}
_APPLIED_DECISIONS: set[str] = set()
# Phase 4 (chat-triggered seam proposals): rejected decisions are the
# evidence pool a proposal cites; BehaviorGraph cannot scan decisions, so
# the gate feeds this registry (rebuilt from the graph on resume).
_REJECTED_DECISIONS: list[dict] = []   # {id, kind, subject_ref, seam_name,
                                       #  resolution_rationale (ADR-026)}
_SEAM_PROPOSED: set[str] = set()       # seam_proposal_request ids handled
_APPROVED_PUBLISH: set[str] = set()
_THREAD_TO_BRANCH: dict[str, str] = {}
# ADR-027: operator continuation directions per branch (from rejected promote
# resolutions), oldest first. Rebuilt from operator_direction observations on
# resume; dispatch stamps the LATEST onto the task it creates.
_OPERATOR_DIRECTIONS: dict[str, list[str]] = {}
_DRAFTED_OBS: set[str] = set()
_FINDING_EMITTED: set[str] = set()
_SLUGS: set[str] = set()
_PUBLISHED_SLUGS: set[str] = set()
# Editorial machinery (ADR-014): the finding queue and its bookkeeping.
_QUEUED_FINDINGS: dict[str, dict] = {}   # finding obs id → provenance context (5a)
_COVERED_FINDINGS: set[str] = set()      # findings a draft request already covers
_RESEARCH_REQUESTED: set[str] = set()    # branch ids with a research request
_DECIDED_THIN: list[str] = []            # decided branches below the evidence bar
_PENDING_PUBLISH: set[str] = set()       # pending publish decision ids (cap)
_IDLE_LOGGED: dict[str, bool] = {"capped": False}  # one idle obs per cap episode
_BRANCH_EVIDENCE: dict[str, list[str]] = {}  # branch id → supported_by targets
# (BehaviorGraph has no relation iteration, so evidence counting inside
# behaviors goes through this registry, fed by relation.created events.)


def clear_lab_registry() -> None:
    """Reset all in-process caches — between fixtures, or to simulate restart."""
    _CRAWLS.clear()
    _CALLS.clear()
    _WEB_PROVIDER_ID.clear()
    _PLANNED_OBS.clear()
    _BRANCH_COUNT["open"] = 0
    _DISPATCHED.clear()
    _GAP_CHECKED.clear()
    _EVALUATED.clear()
    _PENDING_BY_SUBJECT.clear()
    _PENDING_BY_BRANCH.clear()
    _APPLIED_DECISIONS.clear()
    _REJECTED_DECISIONS.clear()
    _OPERATOR_DIRECTIONS.clear()
    _SEAM_PROPOSED.clear()
    _APPROVED_PUBLISH.clear()
    _THREAD_TO_BRANCH.clear()
    _DRAFTED_OBS.clear()
    _FINDING_EMITTED.clear()
    _SLUGS.clear()
    _PUBLISHED_SLUGS.clear()
    _QUEUED_FINDINGS.clear()
    _COVERED_FINDINGS.clear()
    _OBS_PROVENANCE.clear()
    _BRANCH_EVIDENCE.clear()
    _RESEARCH_REQUESTED.clear()
    _DECIDED_THIN.clear()
    _PENDING_PUBLISH.clear()
    _IDLE_LOGGED["capped"] = False
    from .research_worker import clear_research_worker_registry
    clear_research_worker_registry()
    clear_seam_cache()
    # Seam hot-loads mutate live behavior descriptions (module originals AND
    # the bound runtime copies); restore file defaults (prompt body + CHARTER
    # v1 block for the charter behaviors, ADR-018) so fixture runs and
    # resumed boots are isolated from stale overrides.
    for b in list(BEHAVIORS) + list(_LIVE_BEHAVIORS):
        name = getattr(b, "name", "").split(".")[-1]
        if name not in _PROMPTS:
            continue
        default = _file_default_description(name)
        if getattr(b, "description", None) != default:
            try:
                setattr(b, "description", default)
            except Exception:
                object.__setattr__(b, "description", default)
        # Model routing (ADR-019) mutates behavior.model; reset the module
        # originals to None so the next Runtime stamps its provider default
        # and routing re-applies. Bound runtime copies keep their model —
        # apply_model_routing re-stamps them right after any registry reset.
        if any(b is x for x in BEHAVIORS) and getattr(b, "model", None) is not None:
            try:
                setattr(b, "model", None)
            except Exception:
                object.__setattr__(b, "model", None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit_lab_event(graph, event_type: str, payload: dict) -> None:
    """Append a lab marker event (artifact.published, lab.paused, lab.resumed —
    ADR-013/015). Marker events carry no graph-state projection; the lab's own
    projections read them back from the log. Works with both graph flavors:
    BehaviorGraph.emit(type, payload) inside behaviors, Event construction on
    a plain Graph (server paths)."""
    emit = getattr(graph, "emit", None)
    if emit is None:
        return
    try:
        emit(event_type, dict(payload))
        return
    except TypeError:
        pass  # plain Graph: emit(Event)
    try:
        from activegraph.core.event import Event
        emit(Event(id=graph.ids.event(), type=event_type, payload=dict(payload),
                   actor="lab", timestamp=graph.clock.now()))
    except Exception:
        pass


# ---------------------------------------------------------------- text helpers

from html import unescape as _unescape

# Non-content subtrees: their text is chrome, not claims. The live log showed
# nav link rows and SVG path data recorded as "claims" before these were
# dropped (the finding seeded in bundle._seed_findings tells that story).
_DROP_RE = re.compile(
    r"<(script|style|svg|nav|footer)[^>]*>.*?</\1\s*>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")
# Crawlable links are ANCHOR hrefs only. The old bare href= pattern also
# matched <link rel="preload/stylesheet"> asset tags — on a real Next.js
# page that is dozens of same-host /_next/static/... URLs ahead of the nav,
# which would burn the whole page budget on JS chunks and fonts (caught by
# the crawl_stall fixture). And allow '#' inside the href: real-world nav
# links carry fragments ("/docs#install") and the old [^"'#] class could
# never reach the closing quote on those, dropping the link entirely; the
# fragment is stripped after urljoin, so "/docs#install" and "/docs" dedup
# to one page.
_HREF_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"']+)["']""", re.I)

_CLAIM_CUES = (
    "replay", "audit", "event", "graph", "behavior", "pack", "agent",
    "provides", "enables", "supports", "lets", "every", "automatic",
    "deterministic", "fork", "inspect", "%",
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[.,;:!?)\"']*$")


def _strip_html(html: str) -> str:
    """Readable text only: drop script/style/svg/nav/footer subtrees, strip
    remaining tags, decode entities, collapse whitespace."""
    text = _HTML_RE.sub(" ", _DROP_RE.sub(" ", html))
    return re.sub(r"\s+", " ", _unescape(text)).strip()


def _sentence_like(s: str) -> bool:
    """A claim candidate must read like prose: length-bounded, never JSON,
    no markup fragments, mostly real words, sentence-terminated. The live log
    showed raw fetch envelopes ('{"url": ..., "status": 200...') and SVG path
    data pass the old cue check via its any-digit clause; this is the shape
    gate in front of it."""
    if not (30 <= len(s) <= 360):
        return False
    if any(ch in s for ch in "<>{}"):  # markup / JSON fragments
        return False
    if not s.endswith((".", "!", "?")):
        return False
    try:
        json.loads(s)
        return False  # whole candidate parses as JSON — an envelope, not prose
    except ValueError:
        pass
    words = s.split()
    wordish = sum(1 for w in words if _WORD_RE.match(w))
    return len(words) >= 5 and wordish >= max(4, int(0.6 * len(words)))


def _claims_from_text(text: str, cap: int) -> list[str]:
    """Deterministic claim extraction: sentence-like candidates containing
    claim cues. Rejected candidates are dropped silently — the cleanup is not
    an observation; polluting the log with it would be its own pollution."""
    sentences = [re.sub(r"\s+", " ", s.strip())
                 for s in re.split(r"(?<=[.!?])\s+", text)]
    claims = []
    for s in sentences:
        if not _sentence_like(s):
            continue
        low = s.lower()
        if any(cue in low for cue in _CLAIM_CUES) or re.search(r"\d", s):
            claims.append(s)
        if len(claims) >= cap:
            break
    return claims


# DIAGNOSIS — the 1/30 crawl stall (live mission#1: crawl froze at
# fetched=1, queued=0 from evt_768 onward; source#45 is the evidence).
#
# The lab's fetch handler returns {"url", "status", "content"} and
# tool_gateway's call_executor JSON-encodes that whole envelope before
# storing it, truncated to settings.max_output_chars (default 10,000).
# The live activegraph.ai homepage is a few hundred KB of Next.js HTML, so
# the stored envelope was cut off MID-JSON-STRING (source#45 ends inside an
# inline SVG path). From there the chain was:
#
#   1. json.loads(source.content) raised on the truncated envelope, and the
#      fallback treated the RAW ESCAPED ENVELOPE as if it were the page HTML.
#   2. In that escaped text every link reads href=\"...\" — a backslash sits
#      between '=' and the quote — so _HREF_RE matched NOTHING. Zero links
#      were queued (suspect 1: link extraction against real-world HTML, with
#      gateway truncation as the trigger). The queue therefore never had a
#      second page to drain (suspect 2 was a consequence, not a cause), and
#      dedup was clean (suspect 3 ruled out: queued=0, nothing stuck).
#   3. Bonus pollution: claims were extracted from the escaped envelope —
#      the junk site_claim observations (#47–#59) in the live log. Those
#      stay as they are: the log is append-only; the shape gate
#      (_sentence_like) already stops new ones.
#
# Fixtures never caught it because canned pages are tiny — the envelope fit
# under 10K and json.loads always succeeded.
#
# The fix is two layers: bundle.load_lab_packs sizes the gateway's
# max_output_chars so a full fetch envelope survives storage, and this
# parser salvages any envelope that is truncated anyway — recover url and
# status from the intact JSON prefix, then unescape the content fragment by
# re-terminating the JSON string (trimming up to a few chars handles a cut
# inside an escape sequence). Links and claims then come from readable HTML
# instead of escaped soup. Locked by the crawl_stall fixture.
_ENVELOPE_PREFIX_RE = re.compile(
    r'\s*\{\s*"url":\s*"([^"]*)",\s*"status":\s*(\d+),\s*"content":\s*"(.*)\Z',
    re.S)


def _parse_fetch_envelope(content: str) -> tuple[str, Optional[str], Any, Any]:
    """Decode a tool_result's stored fetch envelope, salvaging truncated
    ones. Returns (html, url, status, error); url is None when the stored
    content was not an envelope at all (plain-text handlers)."""
    try:
        payload = json.loads(content)
        if isinstance(payload, dict):
            return (payload.get("content", ""), payload.get("url"),
                    payload.get("status", 200), payload.get("error"))
        return content, None, 200, None
    except ValueError:
        pass
    m = _ENVELOPE_PREFIX_RE.match(content or "")
    if not m:
        return content, None, 200, None
    url, status, fragment = m.group(1), int(m.group(2)), m.group(3)
    for cut in range(0, 7):  # a cut mid-escape leaves a dangling \ or \uXX
        try:
            html = json.loads(f'"{fragment[:len(fragment) - cut]}"')
            break
        except ValueError:
            continue
    else:
        html = fragment.replace('\\"', '"').replace("\\n", " ")
    return html, url, status, None


def _links_from_html(html: str, base_url: str) -> list[str]:
    base_host = urlparse(base_url).netloc
    out = []
    for href in _HREF_RE.findall(html):
        absolute = urljoin(base_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and parsed.netloc == base_host:
            clean = absolute.split("#")[0].rstrip("/")
            if clean and clean not in out:
                out.append(clean)
    return out


# ---------------------------------------------------------------- ingest


def _ensure_web_provider(graph) -> str:
    """Idempotently create the tool_gateway capability_provider for web fetches."""
    if "id" in _WEB_PROVIDER_ID:
        return _WEB_PROVIDER_ID["id"]
    provider = graph.add_object("capability_provider", {
        "name": "web",
        "kind": "local",
        "description": "Lab web fetcher. All lab fetches go through tool_gateway (CONTRACT).",
        "capabilities": ["fetch_url"],
    })
    _WEB_PROVIDER_ID["id"] = provider.id
    return provider.id


def _queue_fetch(graph, mission_id: str, url: str, depth: int) -> None:
    """Propose a low-risk capability_call; tool_gateway approves and executes."""
    state = _CRAWLS[mission_id]
    clean = url.split("#")[0].rstrip("/")
    if clean in state["visited"] or clean in state["queued"]:
        return
    state["queued"].add(clean)
    provider_id = _ensure_web_provider(graph)
    call = graph.add_object("capability_call", {
        "provider_id": provider_id,
        "provider_name": "web",
        "capability_name": "fetch_url",
        "input_data": {"url": clean},
        "risk_class": "low",
        "status": "proposed",
        "proposed_by": "lab.ingest",
        "proposed_at": _now(),
        "metadata": {"lab_crawl": True, "mission_id": mission_id, "depth": depth},
    })
    _CALLS[call.id] = {"url": clean, "depth": depth, "mission_id": mission_id}


@behavior(
    name="ingest",
    on=["object.created"],
    creates=["capability_provider", "capability_call", "observation"],
)
def ingest(event, graph, ctx, *, settings: LabSettings):
    """Crawl the mission's site through tool_gateway and extract claim evidence.

    On: object.created (mission, status=active) — starts the crawl at target_url.
        object.created (source, kind=crawl_request) — queues one extra URL.
        object.created (source, kind=tool_result from the lab's fetch_url calls)
        — records the page: claim observations (metadata.lab=site_claim,
        grounds relation to the source), same-domain links queued to depth
        crawl_max_depth / page cap crawl_page_cap, and a mission progress patch
        per page (the progress event).

    Creates: capability_provider, capability_call, observation.
    The source objects themselves are created by tool_gateway's result_sourcer —
    that is the emergent path: ingest proposes calls, the gateway executes and
    sources, core's observation_extractor adds its generic observations for free.
    """
    obj = event.payload.get("object", {})
    obj_id = obj.get("id")
    obj_type = obj.get("type")
    data = obj.get("data", {})

    # ── Case 1: mission created → start crawl ─────────────────────────────
    if obj_type == "mission":
        if not settings.crawl_enabled:
            return
        target = (data.get("target_url") or "").strip()
        if not target or data.get("status") != "active":
            return
        _CRAWLS.setdefault(obj_id, {"visited": set(), "fetched": 0, "queued": set()})
        _queue_fetch(graph, obj_id, target, depth=0)
        return

    if obj_type != "source":
        return

    # ── Case 2: explicit crawl request → queue one URL ────────────────────
    if data.get("kind") == "crawl_request":
        meta = data.get("metadata") or {}
        mission_id = meta.get("mission_id")
        url = (data.get("url") or data.get("content") or "").strip()
        if not mission_id or not url:
            return
        _CRAWLS.setdefault(mission_id, {"visited": set(), "fetched": 0, "queued": set()})
        _queue_fetch(graph, mission_id, url, depth=int(meta.get("depth", 0)))
        return

    # ── Case 3: fetched page came back through the gateway ────────────────
    if data.get("kind") != "tool_result":
        return
    call_id = (data.get("metadata") or {}).get("call_id")
    call_info = _CALLS.get(call_id)
    if not call_info:
        return  # Not one of the lab's fetches.

    mission_id = call_info["mission_id"]
    depth = call_info["depth"]
    url = call_info["url"]
    state = _CRAWLS.setdefault(mission_id, {"visited": set(), "fetched": 0, "queued": set()})
    state["queued"].discard(url)
    page_cap = effective_setting(graph, settings, "crawl_page_cap")
    max_depth = effective_setting(graph, settings, "crawl_max_depth")
    max_claims = effective_setting(graph, settings, "max_claims_per_page")
    if url in state["visited"] or state["fetched"] >= page_cap:
        return
    state["visited"].add(url)
    state["fetched"] += 1

    # The fetch handler returns JSON {"url", "status", "content", "error"?};
    # the gateway stores it (sanitized, possibly truncated) as the source
    # content. _parse_fetch_envelope salvages truncated envelopes — the
    # 1/30-stall diagnosis lives above its definition.
    content = data.get("content") or ""
    html, env_url, status, fetch_error = _parse_fetch_envelope(content)
    fetched_url = env_url or url

    # A failed page is evidence, not an error: record it and move on. The
    # crawl never aborts on one page.
    if fetch_error or (isinstance(status, int) and (status == 0 or status >= 400)):
        graph.add_object("observation", {
            "text": (f"Fetch failed for {fetched_url}: status={status}"
                     + (f" ({fetch_error})" if fetch_error else "")
                     + ". The page could not be read from this environment."),
            "confidence": 1.0,
            "source_ids": [obj_id],
            "category": "risk",
            "metadata": {"lab": "fetch_failure", "mission_id": mission_id,
                         "url": fetched_url, "status": status},
        })
        html = ""  # no claims, no links from a failed page

    text = _strip_html(html)

    try:
        # Claim observations — the gap list's raw material.
        for claim in _claims_from_text(text, max_claims):
            obs = graph.add_object("observation", {
                "text": claim,
                "confidence": 0.7,
                "source_ids": [obj_id],
                "category": "fact",
                "metadata": {"lab": "site_claim", "mission_id": mission_id, "url": fetched_url},
            })
            graph.add_relation(obj_id, obs.id, "grounds")

        # Same-domain links, depth- and cap-bounded.
        if depth < max_depth:
            budget = page_cap - state["fetched"] - len(state["queued"])
            for link in _links_from_html(html, fetched_url):
                if budget <= 0:
                    break
                if link not in state["visited"] and link not in state["queued"]:
                    _queue_fetch(graph, mission_id, link, depth + 1)
                    budget -= 1
    except Exception as exc:
        # Per-page isolation: one malformed page never aborts the crawl.
        graph.add_object("observation", {
            "text": f"Page processing failed for {fetched_url}: {type(exc).__name__}: {exc}",
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "fetch_failure", "mission_id": mission_id,
                         "url": fetched_url, "status": status},
        })

    # Progress event per page: a mission patch is a committed, projectable event.
    mission = graph.get_object(mission_id)
    if mission is not None:
        meta = dict(mission.data.get("metadata") or {})
        meta["crawl"] = {
            "fetched": state["fetched"],
            "queued": len(state["queued"]),
            "page_cap": page_cap,
            "last_url": fetched_url,
            "progress_interval_seconds": effective_setting(
                graph, settings, "progress_interval_seconds"),
            "seam_versions": seam_versions_stamp(
                graph, "setting.crawl_page_cap", "setting.crawl_max_depth",
                "setting.max_claims_per_page"),
        }
        graph.patch_object(mission_id, {"metadata": meta})


# ---------------------------------------------------------------- plan


@llm_behavior(
    name="plan",
    on=["object.created"],
    where={"object.type": "observation", "object.data.metadata.lab": "site_claim"},
    description=_file_default_description("plan"),
    output_schema=PlanProposal,
    model=None,
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["branch"],
    max_tokens=1024,
    tools=[],
)
def plan(event, graph, ctx, out, *, settings: LabSettings):
    """Propose a branch for a weakly evidenced site claim.

    On: object.created (observation, metadata.lab=site_claim)
    Creates: branch (proposed, gated) + has_branch + supported_by relations.
    The narrated reasoning rides in the branch.created event payload
    (data.metadata.reasoning) — prioritization is prose, never a formula.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    obs_id = obj.get("id")
    data = obj.get("data", {})
    mission_id = (data.get("metadata") or {}).get("mission_id")

    if not obs_id or obs_id in _PLANNED_OBS:
        return
    _PLANNED_OBS.add(obs_id)

    if out is None or not getattr(out, "should_branch", False):
        return
    if _BRANCH_COUNT["open"] >= effective_setting(graph, settings, "max_open_branches"):
        return

    # Phantom-work guard (ADR-032): never propose a branch to build a
    # capability the lab already has.
    phantom = _phantom_capability(out.intent, settings)
    if phantom:
        _record_phantom_suppression(graph, out.intent, phantom, None, mission_id)
        return

    branch = graph.add_object("branch", {
        "title": (out.title or "Untitled branch")[:120],
        "intent": out.intent,
        "status": "proposed",
        "authority": "gated",
        "mission_id": mission_id,
        "metadata": {
            "reasoning": out.reasoning,
            "claim_observation_id": obs_id,
            "proposed_by": "lab.plan",
            "seam_versions": seam_versions_stamp(graph, "prompt.plan",
                                                 "charter.mission"),
        },
    })
    _BRANCH_COUNT["open"] += 1
    if mission_id:
        graph.add_relation(mission_id, branch.id, "has_branch")
    graph.add_relation(branch.id, obs_id, "supported_by")


# ---------------------------------------------------------------- work


# ── routing: verb/intent classification (ADR-006; extends ADR-025's contract) ─
# A task ROUTES BY ITS ACTION, not by the nouns it mentions. Reading source to
# verify/check/examine a claim is RESEARCH — the research_worker has get_file
# and owns it (ADR-022/028). Only tasks that WRITE, MODIFY, or GENERATE code
# route to codebase.code_task. The earlier keyword router (`code|repo|test|
# implement`) sent ANYTHING naming code/a repo to codebase: branch#847, a
# verification task that merely said "fetch the implementation files from the
# repo and verify the claim", was misrouted to a lane with no reactor — the
# production defect this fixes.
#
# Code-write intent is the IMPERATIVE BARE verb form — the lab is COMMANDED to
# produce or change code. The bare form is the whole trick: "implement X" is a
# command; the third-person "X implements Y" and the noun "implementation"
# DESCRIBE a subject and never command the lab (the branch#64 case). Word
# boundaries on bare forms exclude both, the same verb-family discipline
# ADR-025 brought to steering verbs.
# "code" is deliberately NOT a write verb here: it is overwhelmingly the noun
# ("verify THE CODE") — the very token the old keyword router misrouted on. It
# stays in _CODE_OBJECT_RE below as a noun a write verb may act on.
_CODE_WRITE_VERBS = (
    "write", "implement", "build", "create", "modify", "refactor", "edit",
    "patch", "extend", "generate", "develop", "rewrite", "scaffold", "port",
    "migrate", "rename", "construct", "author", "add", "fix", "remove",
    "delete",
)
_CODE_WRITE_RE = re.compile(
    r"\b(?:" + "|".join(_CODE_WRITE_VERBS) + r")\b", re.I)
# A code-write task acts ON a code object: a pack, module, behavior, function,
# endpoint, parser, schema, migration, test to author. A write verb without a
# code object ("create an observation", "add a finding") is not a code task —
# requiring both keeps the verb list from capturing ordinary lab prose. Where a
# keyword list is unavoidable, this is it, and it is documented.
_CODE_OBJECT_RE = re.compile(
    r"\b(?:code|codebase|pack|module|behaviou?r|function|method|class(?:es)?|"
    r"endpoint|adapter|parser|script|schema|migration|tests?|api|cli|"
    r"library|wrapper|handler|sandbox|runtime|compiler|linter)\b", re.I)


def _routing_for_intent(intent: str) -> dict[str, Any]:
    """Routing convention for emergent dispatch (ADR-006), classified by the
    task's ACTION (ADR-025 routing contract, extended).

    research.deep_research  — read/verify/examine source to check a claim; the
                              research_worker fetches it (it has get_file,
                              ADR-022/028). The DEFAULT: a mention of code,
                              files, or a repo is not a request to write code.
    codebase.code_task      — WRITE, MODIFY, or GENERATE code: an imperative
                              code-write verb acting on a code object.

    OPEN (docs/ARCHITECTURE.md): exact tag convention. No upstream pack reacts
    to core tasks at the current pin; the lab-local research worker (ADR-020)
    is the only reactor, so verification work MUST land in its lane.

    Defaulting to research is the deliberate risk posture: over-routing a
    code-write task to the (read-only) research lane is cheap to correct;
    under-routing a READ task to a dead codebase lane is the production defect
    (branch#847 → decision#910's false absence). Reading is the safe default.
    """
    low = (intent or "").lower()
    if _CODE_WRITE_RE.search(low) and _CODE_OBJECT_RE.search(low):
        return {"domain": "codebase", "capability": "code_task"}
    return {"domain": "research", "capability": "deep_research"}


def _available_capabilities(graph, settings) -> set[tuple[str, str]]:
    """The lab's ACTUAL capability lanes — what it can really execute now, not
    what routing happened to tag (ADR-031). A lane is 'available' only when its
    reactor is live with its tools. Consulted before any behavior asserts a
    capability is absent."""
    caps: set[tuple[str, str]] = set()
    try:
        from .research_worker import research_lane_available
        if research_lane_available(settings):
            caps.add(("research", "deep_research"))
    except Exception:
        pass
    return caps


def _available_tools(settings) -> frozenset[str]:
    """The concrete tool names the lab can call right now (ADR-031/032),
    grounded in the research lane's tool list when it is live — the same set
    the capability self-check and the phantom-work guard reason over."""
    try:
        from .research_worker import (RESEARCH_WORKER_TOOLS,
                                      research_lane_available)
        if research_lane_available(settings):
            return RESEARCH_WORKER_TOOLS
    except Exception:
        pass
    return frozenset()


# ── phantom-work guard (ADR-032) ──────────────────────────────────────────────
# A capability-BUILD proposal (a branch proposing to build/extend a pack for
# capability X) must check whether X already exists before being proposed. If
# it does, the proposal is phantom work: suppress it and record an observation
# that the capability is present and the prior gap was spurious. (Production:
# branch#911 proposed building get_file — which shipped in ADR-028 — born from
# the false gap decision#910 asserted. A capability self-check that only fixes
# the verdict still leaves the proposal it spawned.)
#
# The words a build-proposal uses to ASK for a capability the lab already has,
# mapped to the live tool that already provides it. Documented and deliberately
# narrow: the guard fires only when a build verb, a capability noun, AND one of
# these aliases for an AVAILABLE tool all appear.
_EXISTING_CAPABILITY_ALIASES = {
    "github.get_file": (
        "get_file", "get file", "file content", "file contents",
        "files' content", "contents of files", "contents of the files",
        "retrieve file", "retrieving file", "retrieve the file",
        "fetch file", "fetch the file", "read file", "read the file",
        "retrieve file contents", "retrieve the contents", "file retrieval",
        "source file contents", "source-file contents", "retrieve source",
    ),
    "github.get_tree": (
        "get_tree", "get tree", "directory tree", "repo tree",
        "repository tree", "file tree", "directory listing", "list the files",
        "list repository files",
    ),
    "web.fetch_url": (
        "fetch_url", "fetch url", "fetch a url", "fetch web", "web fetch",
        "fetch the page", "fetch web pages", "download the page",
        "fetch a web page",
    ),
}
_BUILD_VERB_RE = re.compile(
    r"\b(?:build|add|create|implement|develop|introduce|provide|enable|"
    r"gain|acquire|construct|stand up|wire up)\b", re.I)
_CAPABILITY_NOUN_RE = re.compile(
    r"\b(?:capab\w*|tool|tooling|pack|ability|means|support|feature|"
    r"mechanism|worker|adapter|integration)\b", re.I)


def _phantom_capability(intent: str, settings: LabSettings) -> Optional[str]:
    """If `intent` is a capability-BUILD proposal for a capability the lab
    ALREADY has, return the existing tool's name; else None (ADR-032)."""
    low = (intent or "").lower()
    if not (_BUILD_VERB_RE.search(low) and _CAPABILITY_NOUN_RE.search(low)):
        return None  # not framed as building a capability
    for tool_name in sorted(_available_tools(settings)):
        for alias in _EXISTING_CAPABILITY_ALIASES.get(tool_name, ()):
            if alias in low:
                return tool_name
    return None


def _record_phantom_suppression(graph, intent: str, tool_name: str,
                                branch_id: Optional[str],
                                mission_id: Optional[str]):
    """A capability-build proposal was suppressed because the capability is
    already present (ADR-032). Record an observation in its place — no branch,
    no phantom work — naming the live tool and flagging the prior gap as
    spurious."""
    text = (
        f"Build-proposal suppressed (phantom work): the proposed capability is "
        f"already present as '{tool_name}'. A branch to build it would "
        f"duplicate a live tool, so any capability gap that prompted this "
        f"proposal was spurious. Proposed intent: "
        f"{(intent or '').strip()[:200]}"
    )
    obs = graph.add_object("observation", {
        "text": text,
        "confidence": 0.95,
        "category": "fact",
        "metadata": {"lab": "phantom_work_suppressed",
                     "existing_tool": tool_name,
                     "lab_branch_id": branch_id,
                     "mission_id": mission_id},
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")
    return obs


def _dispatch_branch(graph, branch_id: str, branch_data: dict, settings: LabSettings) -> None:
    if branch_id in _DISPATCHED:
        return
    _DISPATCHED.add(branch_id)
    intent = branch_data.get("intent") or branch_data.get("title") or ""
    routing = _routing_for_intent(intent)
    meta: dict[str, Any] = {
        "routing": routing,
        "tags": ["lab", routing["domain"]],
        "lab_branch_id": branch_id,
        "progress_contract": {
            "interval_seconds": settings.progress_interval_seconds,
            "uninterruptible": False,
        },
    }
    # ADR-027: a branch carrying operator direction (a rejected promote's
    # resolution_rationale) dispatches it VERBATIM — the worker must be able
    # to read what the operator ordered. The latest activation message rides
    # too: URLs in it steer the worker's sources.
    directions = _OPERATOR_DIRECTIONS.get(branch_id) or []
    if directions:
        meta["operator_direction"] = directions[-1]
    activation_msg = (branch_data.get("metadata") or {}).get("activation_message")
    if activation_msg:
        meta["activation_message"] = activation_msg
    task = graph.add_object("task", {
        "title": (branch_data.get("title") or "Lab task")[:120],
        "description": intent,
        "status": "active",
        "priority": "medium",
        "metadata": meta,
    })
    graph.add_relation(branch_id, task.id, "dispatched")
    if settings.dispatch_gap_check:
        # The probe patch lands one event boundary later — by then, any pack
        # reacting to task.created has already mutated the live graph.
        meta = dict(task.data.get("metadata") or {})
        meta["dispatch_probe"] = True
        graph.patch_object(task.id, {"metadata": meta})


def _task_reacted(graph, task_id: str) -> bool:
    """Did any pack react to the task? Reaction = a relation linking work
    products to the task (core convention: executes/generates), in either
    relation-argument convention (ADR-008, lab_pack/compat.py), or a status
    change away from 'active'. The lab's own research worker claims through
    a registry too (BehaviorGraph cannot iterate relations — ADR-020)."""
    from .compat import decode_relation, relation_touches
    from .research_worker import task_claimed
    if task_claimed(task_id):
        return True
    try:
        for r in graph.relations():
            rel_type, _, _ = decode_relation(r)
            if rel_type in ("executes", "generates") and relation_touches(r, task_id):
                return True
    except Exception:
        pass
    task = graph.get_object(task_id)
    return bool(task and task.data.get("status") not in ("active", None))


def _gap_check(graph, task_id: str, settings: LabSettings) -> None:
    if task_id in _GAP_CHECKED:
        return
    _GAP_CHECKED.add(task_id)
    if _task_reacted(graph, task_id):
        return
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = task.data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    routing = meta.get("routing") or {}
    actual = (routing.get("domain"), routing.get("capability"))

    # Capability self-check (ADR-031): "no pack reacted" is a ROUTING fact, not
    # proof the lab cannot do the work. Before recording absence, consult the
    # actual available-capability set and re-classify the intent: if this
    # task's work belongs to a lane whose capability IS present, this is a
    # MISROUTE — "not reached", never "absent". (Production: decision#910
    # asserted the lab "lacks the means to retrieve file contents", false since
    # get_file shipped in ADR-028; the worker's lane simply was not the one
    # routed. branch#847.)
    available = _available_capabilities(graph, settings)
    intent = task.data.get("description") or task.data.get("title") or ""
    correct = _routing_for_intent(intent)
    correct_key = (correct["domain"], correct["capability"])
    misrouted = correct_key != actual and correct_key in available

    if misrouted:
        gap_text = (
            f"Misrouted, capability available: task '{task.data.get('title')}' "
            f"was routed {actual[0]}.{actual[1]} and no pack reacted, but its "
            f"intent is {correct_key[0]}.{correct_key[1]} work whose capability "
            f"IS present in the lab. Outcome: not reached — a routing miss, not "
            f"a capability absence. Re-dispatch under the corrected routing."
        )
        lab_tag = "routing_miss"
        obs_meta = {"lab": lab_tag, "lab_branch_id": branch_id,
                    "task_id": task_id,
                    "misrouted_from": f"{actual[0]}.{actual[1]}",
                    "correct_routing": f"{correct_key[0]}.{correct_key[1]}"}
    else:
        gap_text = (
            f"Capability gap: no loaded pack reacted to task "
            f"'{task.data.get('title')}' (routing: {actual[0]}.{actual[1]}), "
            f"and no available capability covers this work. The lab cannot "
            f"execute this work yet. A gap is evidence, not an error."
        )
        lab_tag = "capability_gap"
        obs_meta = {"lab": lab_tag, "lab_branch_id": branch_id,
                    "task_id": task_id}

    obs = graph.add_object("observation", {
        "text": gap_text,
        "confidence": 0.95,
        "category": "risk",
        "metadata": obs_meta,
    })
    if branch_id:
        graph.add_relation(branch_id, obs.id, "supported_by")
    # The blocked patch carries the outcome as the task's result_summary;
    # work's outcome path (which now treats `blocked` as an outcome — the
    # branch#64 silent path) turns it into the evaluation interpret fires on.
    new_meta = dict(meta)
    new_meta["result_summary"] = gap_text
    graph.patch_object(task_id, {"status": "blocked", "metadata": new_meta})


def _mark_task_outcome(graph, task_id: str, status: str) -> None:
    if task_id in _EVALUATED:
        return
    _EVALUATED.add(task_id)
    task = graph.get_object(task_id)
    if task is None:
        return
    meta = task.data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    judgment = {"done": "completed_successfully", "blocked": "blocked"}.get(status, "failed")
    graph.add_object("evaluation", {
        "subject_id": task_id,
        "subject_type": "task",
        "judgment": judgment,
        "rationale": (meta.get("result_summary") or "")[:500],
        "evaluator": "lab.work",
        "metadata": {"lab": "task_outcome", "lab_branch_id": branch_id, "task_id": task_id},
    })


@behavior(
    name="work",
    on=["object.created", "patch.applied"],
    creates=["task", "observation", "evaluation"],
)
def work(event, graph, ctx, *, settings: LabSettings):
    """Dispatch activated branches as core tasks; record gaps and outcomes.

    On: branch created/patched to status=active → core task with routing tags
        + dispatched relation + a probe patch.
        patch.applied (the probe) → capability-gap check one event boundary
        after dispatch; no reaction → gap observation + task blocked.
        task patched to done/failed → task-outcome evaluation (interpret's
        trigger).
    Creates: task, observation (capability_gap), evaluation (task_outcome).
    The lab never calls a domain pack — packs react to the task or they don't,
    and either way the graph records it (ADR-006).
    """
    if event.type == "object.created":
        obj = event.payload.get("object", {})
        if obj.get("type") == "branch" and obj.get("data", {}).get("status") == "active":
            _dispatch_branch(graph, obj.get("id"), obj.get("data", {}), settings)
        return

    # patch.applied
    target = event.payload.get("target")
    diff = event.payload.get("diff") or {}
    if not target:
        return
    obj = graph.get_object(target)
    if obj is None:
        return

    if obj.type == "branch":
        status = diff.get("status") or {}
        if status.get("new") == "active":
            _dispatch_branch(graph, target, obj.data, settings)
        return

    if obj.type == "task":
        meta = obj.data.get("metadata") or {}
        if not meta.get("lab_branch_id"):
            return
        status = diff.get("status") or {}
        new = status.get("new")
        if new in ("done", "rejected", "failed", "blocked"):
            # `blocked` is an outcome too (the branch#64 silent path): a
            # gap-blocked or watchdog-released task used to exit the loop
            # here — no task_outcome evaluation, so interpret never fired, no
            # promote decision surfaced, and the branch dangled active with
            # pending stuck at zero. The operator hears about every outcome.
            _mark_task_outcome(graph, target,
                               "done" if new == "done"
                               else "blocked" if new == "blocked" else "failed")
            return
        if "metadata" in diff and meta.get("dispatch_probe") and settings.dispatch_gap_check:
            _gap_check(graph, target, settings)


# ---------------------------------------------------------------- interpret


@llm_behavior(
    name="interpret",
    on=["object.created"],
    where={"object.type": "evaluation", "object.data.metadata.lab": "task_outcome"},
    description=_file_default_description("interpret"),
    output_schema=InterpretSummary,
    model=None,
    view={
        "around": "event.payload.object.data.metadata.lab_branch_id",
        "depth": 1,
        "recent_events": 0,
    },
    creates=["observation", "decision", "branch"],
    max_tokens=1024,
    tools=[],
)
def interpret(event, graph, ctx, out, *, settings: LabSettings):
    """Turn a task outcome into evidence and a gated promote decision.

    On: object.created (evaluation, metadata.lab=task_outcome)
    Creates: observation (interpretation) + supported_by, branch →
    interpreting, decision (promote, pending) — gate takes it from there.
    outcome='follow_up' additionally proposes a child branch.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    task_id = meta.get("task_id")
    if not branch_id or out is None or is_inert(getattr(out, "summary", None)):
        return

    branch = graph.get_object(branch_id)
    if branch is None or branch.data.get("status") in ("decided", "archived"):
        return

    obs = graph.add_object("observation", {
        "text": out.summary,
        "confidence": 0.8,
        "category": "fact",
        "metadata": {"lab": "interpretation", "lab_branch_id": branch_id,
                     "task_id": task_id,
                     "seam_versions": seam_versions_stamp(graph, "prompt.interpret",
                                                          "charter.mission")},
    })
    graph.add_relation(branch_id, obs.id, "supported_by")
    graph.patch_object(branch_id, {"status": "interpreting"})

    evidence = [obs.id]
    if task_id:
        evidence.append(task_id)
    if obj.get("id"):
        evidence.append(obj.get("id"))

    graph.add_object("decision", {
        "subject_ref": branch_id,
        "kind": "promote",
        "status": "pending",
        "rationale": out.summary[:500],
        "evidence_refs": evidence,
        "metadata": {"requested_by": "lab.interpret"},
    })

    if out.outcome == "follow_up" and out.follow_up_intent:
        # Phantom-work guard (ADR-032): a follow-up that proposes building a
        # capability the lab already has is suppressed with an explanatory
        # observation — the false-gap → phantom-proposal chain (branch#911)
        # stops here, not just at the verdict.
        phantom = _phantom_capability(out.follow_up_intent, settings)
        if phantom:
            _record_phantom_suppression(graph, out.follow_up_intent, phantom,
                                        branch_id, branch.data.get("mission_id"))
        elif _BRANCH_COUNT["open"] < settings.max_open_branches:
            child = graph.add_object("branch", {
                "title": f"Follow-up: {out.follow_up_intent[:100]}",
                "intent": out.follow_up_intent,
                "status": "proposed",
                "authority": "gated",
                "parent_branch_id": branch_id,
                "mission_id": branch.data.get("mission_id"),
                "metadata": {"reasoning": out.summary, "proposed_by": "lab.interpret"},
            })
            _BRANCH_COUNT["open"] += 1
            graph.add_relation(child.id, branch_id, "forked_from")


# ---------------------------------------------------------------- digest
# Editorial machinery (ADR-014): findings accumulate, drafts are requested.
# A draft_request observation is the ONLY trigger draft_writer reacts to;
# its data carries the code-injected context the model drafts from — the
# classification guidance (4a) and per-item provenance (5a) live HERE, in
# prompt-assembly code, never in prompt prose.

_CLASSIFICATION_GUIDANCE = (
    "Classification guidance: post_kind is 'note' for a small post covering "
    "one finding or a digest of accumulated findings; 'research' for a "
    "multi-evidence or multi-branch synthesis from decided branches; 'build' "
    "for a post about constructing the lab itself (runtime, gates, seams, "
    "deploys)."
)

_OBS_PROVENANCE: dict[str, dict] = {}  # obs id → provenance context (5a)


def _observation_provenance(graph, obs_id: str, data: dict, event) -> dict:
    """5a: per-item context injected into every draft request — originating
    branch title, when and by what behavior the observation was created, and
    whether it was seeded or arose from live work. Captured at creation time:
    the triggering event IS the observation's object.created event, so actor
    and timestamp are right there (behaviors cannot scan the log)."""
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    branch = graph.get_object(branch_id) if branch_id else None
    actor = str(getattr(event, "actor", None) or "system")
    origin = ("seeded" if actor == "system"
              else f"live work by the {actor} behavior")
    return {
        "finding_id": obs_id,
        "text": (data.get("text") or "")[:500],
        "branch_id": branch_id,
        "branch_title": (branch.data.get("title") if branch is not None else None),
        "created_at": str(getattr(event, "timestamp", "") or ""),
        "created_by": actor,
        "origin": origin,
        "evidence_refs": list(meta.get("evidence_refs") or []),
    }


def _item_context(graph, ref: str) -> dict:
    """Provenance context for a draft-request item, registry first, graph
    fallback for objects that predate provenance tracking."""
    ctx = _OBS_PROVENANCE.get(ref)
    if ctx:
        return ctx
    o = graph.get_object(ref)
    text = ""
    out = {"finding_id": ref,
           "origin": "unknown (predates provenance tracking)"}
    if o is not None:
        text = (o.data.get("text") or o.data.get("title")
                or o.data.get("rationale") or "")[:300]
        rr = (o.data.get("metadata") or {}).get("resolution_rationale")
        if rr:
            # ADR-026: a cited decision carries the OPERATOR's resolution
            # reason alongside the proposer's pitch.
            out["resolution_rationale"] = str(rr)[:300]
    out["text"] = text
    return out


def _drafting_capped(graph, settings: LabSettings, *, operator: bool) -> bool:
    """4c: when the inbox already holds max_drafts_pending publish decisions,
    automatic drafting idles and records ONE observation per cap episode.
    Operator-requested drafts bypass the cap (explicit attention, ADR-014)."""
    if operator:
        return False
    cap = effective_setting(graph, settings, "max_drafts_pending")
    if len(_PENDING_PUBLISH) < cap:
        _IDLE_LOGGED["capped"] = False
        return False
    if not _IDLE_LOGGED["capped"]:
        _IDLE_LOGGED["capped"] = True
        graph.add_object("observation", {
            "text": (f"Drafting idles: {len(_PENDING_PUBLISH)} publish decisions "
                     f"already pending (cap {cap}). The operator's attention is "
                     "also a budget — findings stay queued until the inbox drains."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "drafting_idle",
                         "pending_publish": len(_PENDING_PUBLISH), "cap": cap},
        })
    return True


def _request_draft(graph, *, post_kind_hint: str, item_ids: list[str],
                   requested_by: str, branch_id: Optional[str] = None,
                   mission_id: Optional[str] = None, rationale: str = "",
                   operator_brief: Optional[str] = None):
    """Create the draft_request observation draft_writer triggers on. Its
    data is the assembled draft context: classification guidance (4a) and
    per-item provenance (5a) — the model can no longer not know where a
    finding came from.

    `operator_brief` (the seam-truncation fix, mirrored): an operator's
    drafting message rides VERBATIM — in metadata and as a delimited
    OPERATOR BRIEF block in the request text the model reads. The brief
    governs scope; the listed items become available evidence, not the
    mandatory skeleton. evt_13857's commission ('Draft a research-kind post
    about the rejection-to-self-modification loop…') was compressed to
    'Operator requested a draft' + 160 chars, and observation#714 drafted a
    14-finding digest instead."""
    contexts = [_item_context(graph, i) for i in item_ids]
    evidence: list[str] = []
    for c in contexts:
        for r in [c.get("finding_id")] + list(c.get("evidence_refs") or []):
            if r and r not in evidence:
                evidence.append(r)
    text = (f"Draft request ({post_kind_hint}): write one {post_kind_hint} "
            f"post covering {len(item_ids)} item(s). {rationale} "
            + _CLASSIFICATION_GUIDANCE)
    meta = {"lab": "draft_request", "draft_request": True,
            "post_kind_hint": post_kind_hint,
            "finding_ids": list(item_ids),
            "evidence_refs": evidence,
            "findings_context": contexts,
            "lab_branch_id": branch_id,
            "mission_id": mission_id,
            "requested_by": requested_by}
    if operator_brief and operator_brief.strip():
        meta["operator_brief"] = operator_brief
        text += ("\n\nOPERATOR BRIEF (verbatim — this brief governs the "
                 "post's scope and content; the listed findings are "
                 "available evidence, not a mandatory skeleton):\n"
                 + operator_brief)
    req = graph.add_object("observation", {
        "text": text,
        "confidence": 1.0,
        "category": "fact",
        "metadata": meta,
    })
    for i in item_ids:
        _COVERED_FINDINGS.add(i)
        graph.add_relation(req.id, i, "covers")
    return req


@behavior(
    name="digest",
    on=["object.created", "relation.created"],
    creates=["observation"],
)
def digest(event, graph, ctx, *, settings: LabSettings):
    """Queue findings; request ONE combined note when enough accumulate.

    On: object.created (observation). Every lab observation's provenance is
    captured here (actor + timestamp from its own creation event — 5a).
    A finding-tagged observation is QUEUED (a queued_finding relation plus
    the registry), never drafted directly. When unpublished queued findings
    reach setting.digest_min_findings, one note-kind draft_request covers
    them all — unless the pending-publish cap idles drafting (4c).
        relation.created (supported_by) — feeds the branch-evidence registry
    the research threshold counts against (BehaviorGraph cannot iterate
    relations; rebuilt from the graph on resume).
    """
    if event.type == "relation.created":
        from .compat import decode_relation
        rel = event.payload.get("relation") or {}
        try:
            rtype, src, tgt = decode_relation(rel)
        except Exception:
            return
        if rtype == "supported_by":
            bucket = _BRANCH_EVIDENCE.setdefault(src, [])
            if tgt not in bucket:
                bucket.append(tgt)
        return

    obj = event.payload.get("object", {})
    if obj.get("type") != "observation":
        return
    obs_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    if not obs_id or obs_id in _OBS_PROVENANCE:
        return
    if meta.get("lab"):
        _OBS_PROVENANCE[obs_id] = _observation_provenance(graph, obs_id, data, event)
    if not meta.get("finding"):
        return
    _QUEUED_FINDINGS[obs_id] = _OBS_PROVENANCE.get(obs_id) or {"finding_id": obs_id}
    mission_id = meta.get("mission_id")
    if mission_id:
        graph.add_relation(mission_id, obs_id, "queued_finding")
    queued = [f for f in _QUEUED_FINDINGS if f not in _COVERED_FINDINGS]
    if len(queued) < effective_setting(graph, settings, "digest_min_findings"):
        return
    if _drafting_capped(graph, settings, operator=False):
        return
    # If every queued finding came from one branch, the digest belongs to it
    # (post provenance then links the thread); mixed origins stay mission-level.
    branch_ids = {(_QUEUED_FINDINGS.get(f) or {}).get("branch_id") for f in queued}
    sole_branch = branch_ids.pop() if len(branch_ids) == 1 else None
    _request_draft(
        graph, post_kind_hint="note", item_ids=queued,
        requested_by="lab.digest", branch_id=sole_branch, mission_id=mission_id,
        rationale=f"{len(queued)} unpublished findings have accumulated.",
    )


# ---------------------------------------------------------------- gate


def _branch_evidence_ids(graph, branch_id: str) -> list[str]:
    """Evidence objects linked supported_by to a branch (ADR-008 decode).
    Inside behaviors the graph is a restricted BehaviorGraph with no relation
    iteration — there the registry (fed by relation.created events, rebuilt
    on resume) is the source."""
    from .compat import decode_relation
    if hasattr(graph, "relations"):
        out = []
        try:
            for r in graph.relations():
                rel_type, src, tgt = decode_relation(r)
                if rel_type == "supported_by" and src == branch_id:
                    out.append(tgt)
            return out
        except Exception:
            pass
    return list(_BRANCH_EVIDENCE.get(branch_id, []))


def _maybe_research_request(graph, branch_id: str, decision_id: str,
                            rationale: str, settings: LabSettings) -> None:
    """4b: research/build drafts are earned, not automatic. A branch reaching
    decided with >= research_min_evidence linked evidence objects gets ONE
    research draft_request; a thinner decided branch waits until >=2 decided
    branches can be synthesized with combined evidence over the same bar.
    Replaces the old fire-per-finding path."""
    if branch_id in _RESEARCH_REQUESTED:
        return
    min_ev = effective_setting(graph, settings, "research_min_evidence")
    evidence = _branch_evidence_ids(graph, branch_id)
    branch = graph.get_object(branch_id)
    title = branch.data.get("title") if branch else branch_id
    mission_id = branch.data.get("mission_id") if branch else None

    if len(evidence) >= min_ev:
        if _drafting_capped(graph, settings, operator=False):
            return
        _RESEARCH_REQUESTED.add(branch_id)
        _request_draft(
            graph, post_kind_hint="research", item_ids=evidence,
            requested_by="lab.gate", branch_id=branch_id, mission_id=mission_id,
            rationale=(f"Branch '{title}' reached decided with "
                       f"{len(evidence)} evidence objects. {rationale[:200]}"),
        )
        return

    # Thin decided branch: hold for synthesis across >=2 decided branches.
    if branch_id not in _DECIDED_THIN:
        _DECIDED_THIN.append(branch_id)
    if len(_DECIDED_THIN) < 2:
        return
    combined: list[str] = []
    for b in _DECIDED_THIN:
        combined += [e for e in _branch_evidence_ids(graph, b) if e not in combined]
    if len(combined) < min_ev:
        return
    if _drafting_capped(graph, settings, operator=False):
        return
    group = list(_DECIDED_THIN)
    _DECIDED_THIN.clear()
    for b in group:
        _RESEARCH_REQUESTED.add(b)
    _request_draft(
        graph, post_kind_hint="research", item_ids=combined,
        requested_by="lab.gate", branch_id=branch_id, mission_id=mission_id,
        rationale=(f"Synthesis across {len(group)} decided branches with "
                   f"{len(combined)} combined evidence objects."),
    )


def _mirror_path(settings: LabSettings, slug: str) -> Path:
    return Path(settings.drafts_dir) / f"{slug}.md"


def _mark_draft_rejected(graph, artifact_id: str, settings: LabSettings) -> None:
    """Rejected drafts keep their mirror file, prefixed with a REJECTED header.
    OPEN (docs/ARCHITECTURE.md): spec wanted artifact status 'archived', but the
    core artifact enum has no such value and core is not ours to change
    (ADR-005) — 'rejected' is the conservative mapping."""
    artifact = graph.get_object(artifact_id)
    if artifact is None:
        return
    slug = (artifact.data.get("metadata") or {}).get("slug")
    if not slug:
        return
    try:
        path = _mirror_path(settings, slug)
        if path.exists():
            body = path.read_text()
            if not body.startswith("REJECTED"):
                path.write_text(f"REJECTED — publish decision rejected; kept for the record.\n\n{body}")
    except OSError:
        pass


def _publish_artifact(graph, artifact_id: str, decision_id: str) -> None:
    """The publishing last mile (Phase 1, ADR-013): an approved publish
    decision patches the artifact to status=published with a published_at
    stamp and appends an artifact.published marker event. Idempotent — an
    already-published artifact keeps its original timestamp and slug, and no
    second event is emitted. Slug uniqueness is enforced HERE, at publish
    time: a collision with an already-published slug gets a numeric suffix
    (registry-backed; rebuilt from the graph on resume)."""
    artifact = graph.get_object(artifact_id)
    if artifact is None:
        return
    meta = dict(artifact.data.get("metadata") or {})
    if artifact.data.get("status") == "published":
        if meta.get("slug"):
            _PUBLISHED_SLUGS.add(meta["slug"])
        return
    slug = meta.get("slug") or "post"
    base, n = slug, 2
    while slug in _PUBLISHED_SLUGS:
        slug = f"{base}-{n}"
        n += 1
    _PUBLISHED_SLUGS.add(slug)
    published_at = _now()
    meta["slug"] = slug
    meta["published_at"] = published_at
    try:
        graph.patch_object(artifact_id, {"status": "published", "metadata": meta})
    except Exception:
        return
    emit_lab_event(graph, "artifact.published", {
        "artifact_id": artifact_id,
        "slug": slug,
        "title": artifact.data.get("title"),
        "published_at": published_at,
        "decision_id": decision_id,
    })


def index_pending_decision(graph, decision_id: str, data: dict) -> None:
    """ADR-025: chat approve/reject keys by the thread's BRANCH, so every
    pending decision is indexed under its branch too — subject_ref for
    promote decisions (the subject IS the branch), metadata.lab_branch_id
    for publish and self_modify decisions. Also used by the server's
    resume rebuild."""
    subject = data.get("subject_ref") or ""
    branch_id = None
    subj = graph.get_object(subject) if subject else None
    if subj is not None and subj.type == "branch":
        branch_id = subject
    elif (data.get("metadata") or {}).get("lab_branch_id"):
        branch_id = (data.get("metadata") or {}).get("lab_branch_id")
    if branch_id:
        bucket = _PENDING_BY_BRANCH.setdefault(branch_id, [])
        if decision_id not in bucket:
            bucket.append(decision_id)


def _apply_decision(graph, decision_id: str, data: dict, settings: LabSettings) -> None:
    if decision_id in _APPLIED_DECISIONS:
        return
    _APPLIED_DECISIONS.add(decision_id)
    subject = data.get("subject_ref")
    kind = data.get("kind")
    status = data.get("status")
    _PENDING_BY_SUBJECT.pop(subject, None)
    for bucket in _PENDING_BY_BRANCH.values():
        if decision_id in bucket:
            bucket.remove(decision_id)
    if status == "rejected":
        meta = data.get("metadata") or {}
        _REJECTED_DECISIONS.append({"id": decision_id, "kind": kind,
                                    "subject_ref": subject,
                                    "seam_name": meta.get("seam_name"),
                                    # ADR-026: the OPERATOR's reason rides
                                    # with the registry entry, so proposals
                                    # cite it, not just the proposer's pitch.
                                    "resolution_rationale":
                                        meta.get("resolution_rationale")})

    if kind == "promote" and subject:
        # ADR-027: a rejected promote no longer archives. Rejection is the
        # operator teaching through the gate — the branch lands on `decided`
        # either way, and the resolution_rationale (when given) becomes an
        # operator_direction observation in the branch's evidence, which a
        # later activation dispatches verbatim. The old reject→archived path
        # buried decision#266's continuation direction with branch#62
        # (evt_13850): the first operator attempt to steer a continuation
        # archived the student instead.
        try:
            graph.patch_object(subject, {
                "status": "decided",
                "metadata": {**((graph.get_object(subject).data.get("metadata")) or {}),
                             "decision_id": decision_id},
            })
        except Exception:
            pass
        if status == "rejected":
            meta = data.get("metadata") or {}
            direction = (meta.get("resolution_rationale") or "").strip()
            if direction:
                obs = graph.add_object("observation", {
                    "text": direction,
                    "confidence": 1.0,
                    "category": "fact",
                    "metadata": {"lab": "operator_direction",
                                 "lab_branch_id": subject,
                                 "decision_id": decision_id},
                })
                graph.add_relation(subject, obs.id, "supported_by")
                _OPERATOR_DIRECTIONS.setdefault(subject, []).append(direction)
        if status == "approved":
            _maybe_research_request(graph, subject, decision_id,
                                    data.get("rationale") or "", settings)
    elif kind == "publish" and subject:
        _PENDING_PUBLISH.discard(decision_id)
        if status == "approved":
            _APPROVED_PUBLISH.add(subject)
            _publish_artifact(graph, subject, decision_id)
        else:
            try:
                graph.patch_object(subject, {"status": "rejected"})
            except Exception:
                pass
            _mark_draft_rejected(graph, subject, settings)
    elif kind == "self_modify" and subject:
        # The gate treats self_modify exactly like publish: absolute. Approval
        # hot-loads the seam (no restart); graph-code drafts stay dormant
        # behind LAB_ALLOW_GRAPH_CODE regardless (ADR-012).
        artifact = graph.get_object(subject)
        a_kind = artifact.data.get("kind") if artifact else None
        if status == "approved":
            try:
                graph.patch_object(subject, {"status": "approved"})
            except Exception:
                pass
            if a_kind == "seam":
                from .seams import hot_load
                hot_load(graph, subject)
        else:
            # A rejected seam was never active — nothing to unload; the
            # cache keeps serving whatever was approved before (or nothing).
            try:
                graph.patch_object(subject, {"status": "rejected"})
            except Exception:
                pass
    # schema_change / dependency_pin / other: the record itself is the outcome;
    # humans act on it outside the runtime (e.g. bump the pin in pyproject).


@behavior(
    name="gate",
    on=["object.created", "patch.applied"],
    creates=["observation"],
)
def gate(event, graph, ctx, *, settings: LabSettings):
    """Surface pending decisions and apply resolved ones. Enforce publish gating.

    On: decision created (pending) → approval-request event (a decision patch
        stamping approval_requested_at; the feed pins it as the inbox).
        decision patched to approved/rejected → outcome applied (promote →
        branch decided either way, a rejection recording its direction as
        evidence — ADR-027; publish → artifact published/rejected).
        artifact created/patched to status=published WITHOUT an approved
        publish decision → reverted to proposed + violation observation.
    NOTHING publishes or self-modifies without an approved decision.
    """
    if event.type == "object.created":
        obj = event.payload.get("object", {})
        data = obj.get("data", {})
        if obj.get("type") == "decision" and data.get("status") == "pending":
            decision_id = obj.get("id")
            _PENDING_BY_SUBJECT[data.get("subject_ref", "")] = decision_id
            index_pending_decision(graph, decision_id, data)
            if data.get("kind") == "publish":
                _PENDING_PUBLISH.add(decision_id)  # the drafting cap's counter
            meta = dict(data.get("metadata") or {})
            meta["approval_requested_at"] = _now()
            graph.patch_object(decision_id, {"metadata": meta})
            return
        if obj.get("type") == "artifact" and data.get("status") == "published":
            _revert_unapproved_publish(graph, obj.get("id"))
        return

    target = event.payload.get("target")
    diff = event.payload.get("diff") or {}
    if not target:
        return
    obj = graph.get_object(target)
    if obj is None:
        return

    if obj.type == "decision":
        new_status = (diff.get("status") or {}).get("new")
        if new_status in ("approved", "rejected"):
            _apply_decision(graph, target, obj.data, settings)
        return

    if obj.type == "artifact":
        if (diff.get("status") or {}).get("new") == "published":
            if target not in _APPROVED_PUBLISH:
                _revert_unapproved_publish(graph, target)


def _revert_unapproved_publish(graph, artifact_id: str) -> None:
    graph.patch_object(artifact_id, {"status": "proposed"})
    graph.add_object("observation", {
        "text": (
            f"Gate violation: artifact {artifact_id} was set to published without "
            "an approved publish decision. Reverted to proposed."
        ),
        "confidence": 1.0,
        "category": "risk",
        "metadata": {"lab": "gate_violation", "artifact_id": artifact_id},
    })


# ---------------------------------------------------------------- draft_writer


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "draft").lower()).strip("-")[:60] or "draft"
    base, n = slug, 2
    while slug in _SLUGS:
        slug = f"{base}-{n}"
        n += 1
    _SLUGS.add(slug)
    return slug


_FOOTNOTE_RE = re.compile(r"\[\^[^\]]+\]")

# ADR-029: the provenance footer records the ACTUAL per-behavior model used,
# not one flattened constant. The pipeline is per-role (draft_writer/plan on
# the deliberate plane, answer/default on the fast plane — ADR-019); a single
# "model X" footer rounded that routing up to one name. These are the LLM
# behaviors whose output can become an artifact's evidence.
_LLM_BEHAVIOR_NAMES = frozenset((
    "draft_writer", "interpret", "plan", "research_worker", "answer",
    "seam_writer"))


_ENTITY_ID_RE = re.compile(
    r"\b(?:observation|evaluation|task|branch|decision|artifact|source)#\d+")


def _footnoted_entity_ids(body: str) -> list[str]:
    """The entity ids the BODY actually footnotes — read from footnote
    DEFINITION lines (`[^id]: …observation#42…`), in first-seen order. Phase 5
    (ADR-029): under an operator brief the decision's evidence_refs and the
    provenance block derive from these, not the queued-findings pile."""
    ids: list[str] = []
    for line in re.findall(r"(?m)^\[\^[^\]]+\]:\s*(.+)$", body or ""):
        for m in _ENTITY_ID_RE.findall(line):
            if m not in ids:
                ids.append(m)
    return ids


def _actor_behavior(actor: Optional[str]) -> Optional[str]:
    """Map an event actor ('lab.research_worker') to its behavior name, or
    None if the creator was not an LLM behavior (system, ingest, digest…)."""
    name = (actor or "").rsplit(".", 1)[-1]
    return name if name in _LLM_BEHAVIOR_NAMES else None


def _provenance_model_footer(meta: dict) -> str:
    """The per-behavior model split for the footer: draft_writer always, plus
    every LLM behavior that produced the cited evidence — each resolved to the
    model actually recorded for it on its llm.responded events (via the
    session's model_by_behavior cache, which carries the same value). Falls
    back to last_model for any behavior with no recorded call (e.g. fixtures
    on the raw mock)."""
    usage = llm_usage()
    model_map = usage.get("model_by_behavior") or {}
    fallback = usage.get("last_model") or "mock"
    contributors = ["draft_writer"]
    for ctx in (meta.get("findings_context") or []):
        name = _actor_behavior(ctx.get("created_by"))
        if name and name not in contributors:
            contributors.append(name)
    pairs = []
    for name in contributors:
        model = model_map.get(name) or (fallback if name == "draft_writer" else None)
        if model:
            pairs.append(f"{name}={model}")
    return ", ".join(pairs) if pairs else fallback

# 5b: first-person process claims — "I was reading…", "during my review…",
# "I examined…" — are narrative about HOW the lab worked. Without an evidence
# ref to a matching event they are invention: the lab's findings were often
# seeded or produced by other behaviors, and the model must not imply
# firsthand process it never performed.
_PROCESS_CLAIM_RE = re.compile(
    r"\bI was (?:reading|reviewing|examining|looking|browsing|crawling|digging)\b"
    r"|\bduring my (?:review|reading|examination|investigation|crawl|research)\b"
    r"|\bI (?:examined|reviewed|inspected|read through|sat down|dug into|"
    r"went through|looked over|combed through)\b",
    re.I)


# Phase 4 (ADR-029): a "specific factual claim" — a digit, a percentage, a
# comparative/quantitative term, a code identifier (snake_case / camelCase),
# an object-id reference, or a URL. A FIRST paragraph carrying none of these
# is framing/intro and is exempt from the coverage flag, so a FIRED check
# means a real unfootnoted SUBSTANTIVE claim rather than boilerplate the
# escape hatch was overriding in 100% of posts.
_FACTUAL_CLAIM_RE = re.compile(
    r"\d|%"
    r"|\b(?:more|less|fewer|faster|slower|larger|smaller|higher|lower|"
    r"increased?|decreased?|reduced?|doubled|halved|outperforms?|than|"
    r"percent|times|fold|exactly)\b"
    r"|\b\w+_\w+\b"             # snake_case identifier
    r"|[A-Za-z0-9_]+#\d+"       # object id reference (observation#42)
    r"|https?://", re.I)
_CAMELCASE_RE = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b")  # interior cap


def _makes_factual_claim(para: str) -> bool:
    return bool(_FACTUAL_CLAIM_RE.search(para)
                or _CAMELCASE_RE.search(para))


def _coverage_review(body: str) -> Optional[str]:
    """Claims-coverage check (draft contract): any substantive paragraph with
    zero evidence refs gets flagged in a review note — never silently
    accepted. Phase 4 (ADR-029): a pure framing/intro OPENING paragraph (the
    first substantive paragraph, if it makes no specific factual claim) is
    exempt — the check fired on paragraph 1 in 100% of posts and was routinely
    overridden, training the guardrail into noise. Extension (5b): first-person
    process claims without a footnote are flagged separately as possible
    invented narrative. Orphan guard: footnotes DEFINED but never cited are
    dead provenance (artifact#718 shipped an unused [^1])."""
    flagged = []
    process_flagged = []
    seen_body = False
    for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", body or "")):
        if not para or para.startswith("#") or para.startswith("[^"):
            continue
        has_footnote = bool(_FOOTNOTE_RE.search(para))
        if _PROCESS_CLAIM_RE.search(para) and not has_footnote:
            process_flagged.append(i + 1)
        if len(para) < 80:
            continue  # headings, transitions — not claims
        is_first_body = not seen_body
        seen_body = True
        if not has_footnote:
            # The opening framing paragraph gets one exemption — but only if it
            # makes no specific factual claim. A substantive opener still flags.
            if is_first_body and not _makes_factual_claim(para):
                continue
            flagged.append(i + 1)
    # Defined-but-uncited footnotes: a definition line ([^id]: …) whose id is
    # cited nowhere in the prose. The definition's own marker is followed by
    # ':' and therefore never counts as a citation.
    defined = re.findall(r"(?m)^\[\^([^\]]+)\]:", body or "")
    cited = set(re.findall(r"\[\^([^\]]+)\](?!:)", body or ""))
    orphaned = [d for d in defined if d not in cited]
    notes = []
    if flagged:
        notes.append(
            "> **Review note (claims coverage):** paragraph(s) "
            + ", ".join(map(str, flagged))
            + " carry no evidence footnotes. Verify or cut before approving.")
    if process_flagged:
        notes.append(
            "> **Review note (process claims):** paragraph(s) "
            + ", ".join(map(str, process_flagged))
            + " make first-person process claims (“I was reading…”, "
            "“during my review…”) with no evidence ref to a matching "
            "event — possible invented narrative. The injected draft context "
            "says where each finding actually came from; verify or cut.")
    if orphaned:
        notes.append(
            "> **Review note (orphan footnotes):** footnote(s) "
            + ", ".join(f"[^{o}]" for o in orphaned)
            + " are defined but never cited in the text — dead provenance. "
            "Cite or cut before approving.")
    if not notes:
        return None
    return "\n\n" + "\n\n".join(notes)


# Phase 1 (ADR-033): overclaim lint — a sibling to the coverage check. The
# project's founding sin is the narrative layer claiming more than the graph
# supports: published posts said "five independent sources" over five
# same-author artifacts and "fully autonomous" inside an operator-steered
# pipeline. This check flags overclaiming language whose claim the graph
# evidence CONTRADICTS — for operator attention in the review note, NEVER
# auto-blocked (the operator judges; the ADR-014 escape-hatch spirit). The
# grounding is the discriminator: each flag fires only when the graph actually
# undercuts the phrasing, so a hedged-correctly draft passes clean.
_INDEPENDENT_RE = re.compile(
    r"\b(?:independent(?:ly)?|separate sources|corroborat\w+|"
    r"multiple (?:independent )?sources|cross-?checked|triangulat\w+)\b", re.I)
_AUTONOMOUS_RE = re.compile(
    r"\b(?:fully autonomous|autonomous(?:ly)?|unprompted|unsupervised|"
    r"of its own accord|without (?:any )?(?:human|operator) "
    r"(?:input|prompting|involvement|intervention|steering))\b", re.I)
_DEMONSTRATED_RE = re.compile(
    r"\b(?:verified|proven|proves?|proved|demonstrat\w+|"
    r"conclusively (?:shows?|showed)|established that)\b", re.I)
_SUPERLATIVE_RE = re.compile(
    r"\b(?:first[- ]ever|the (?:only|first|best|fastest)|only one|"
    r"never before|unprecedented|world'?s (?:first|best|fastest)|"
    r"the most \w+)\b", re.I)


def _source_origin(o) -> Optional[str]:
    """A source's origin signature: an explicit author, else its URL site.
    Two sources with the same signature are NOT independent."""
    if o is None:
        return None
    d = o.data
    m = d.get("metadata") or {}
    author = m.get("author") or d.get("author")
    if author:
        return "author:" + str(author).strip().lower()
    url = d.get("url") or m.get("url") or ""
    if url:
        net = urlparse(url).netloc.lower()
        if net:
            return "site:" + net
    return None


def _evidence_origin(graph, ref: str, ctx: Optional[dict]) -> Optional[str]:
    """Origin signature for one cited evidence id: a source's own origin, the
    joined origins of an observation's source_ids, else the actor/behavior that
    created it. None when it cannot be resolved (then independence stays
    unjudged — the lint never flags on missing data)."""
    o = graph.get_object(ref)
    if o is not None:
        if o.type == "source":
            return _source_origin(o)
        sids = o.data.get("source_ids") or []
        sites = sorted({s for s in (_source_origin(graph.get_object(x))
                                    for x in sids) if s})
        if sites:
            return "+".join(sites)
    if ctx and ctx.get("created_by"):
        return "actor:" + str(ctx["created_by"])
    if o is not None:
        cb = (o.data.get("metadata") or {}).get("created_by")
        if cb:
            return "actor:" + str(cb)
    return None


def _has_demonstration(graph, evidence_ids: list[str]) -> bool:
    """True if any cited evidence is a DEMONSTRATION (an evaluation, or an
    observation that measured/tested), not mere DESCRIPTION. 'verified'/'proven'
    needs a demonstration; a pile of descriptive observations does not earn it."""
    for ref in evidence_ids:
        o = graph.get_object(ref)
        if o is None:
            continue
        if o.type == "evaluation":
            return True
        if o.type == "observation":
            cat = (o.data.get("category") or "").lower()
            if cat in ("measurement", "benchmark", "test", "experiment", "result"):
                return True
    return False


def _operator_in_chain(graph, branch_id: Optional[str],
                       findings_context: list) -> bool:
    """True if an operator message or activation is in the branch's causal
    chain — which makes 'autonomous'/'unprompted' an overclaim. Read without
    iteration: the branch's activation_message (set by the activate verb), the
    operator-direction registry, and the cited findings' creators."""
    if branch_id:
        b = graph.get_object(branch_id)
        if b is not None and (b.data.get("metadata") or {}).get("activation_message"):
            return True
        if _OPERATOR_DIRECTIONS.get(branch_id):
            return True
    for ctx in findings_context or []:
        if "operator" in str(ctx.get("created_by") or "").lower():
            return True
    return False


def _overclaim_review(body: str, graph, evidence_ids: list[str], *,
                      branch_id: Optional[str] = None,
                      findings_context: Optional[list] = None) -> Optional[str]:
    """Flag overclaiming language the graph evidence does not support. Advisory
    only — flagged phrases go in the review note for operator attention, never
    auto-blocked. Each check is graph-grounded so a correctly hedged draft
    passes: 'independent' over single-origin evidence, 'autonomous' where an
    operator is in the chain, 'verified'/'proven' over description-only
    evidence, and superlatives with no supporting footnote."""
    findings_context = findings_context or []
    ctx_by_id = {c.get("finding_id"): c for c in findings_context
                 if c.get("finding_id")}
    flags: list[tuple[str, str]] = []

    if _INDEPENDENT_RE.search(body or "") and len(evidence_ids) >= 2:
        origins = [_evidence_origin(graph, r, ctx_by_id.get(r))
                   for r in evidence_ids]
        resolved = [o for o in origins if o]
        # Only flag when EVERY cited item resolved and they collapse to one
        # origin — independence claimed over evidence that shares an author.
        if resolved and len(resolved) == len(evidence_ids) \
                and len(set(resolved)) == 1:
            flags.append((
                "independent sources",
                f"the draft calls its evidence independent, but the "
                f"{len(evidence_ids)} cited items share one origin "
                f"({resolved[0]}) — same-origin artifacts are not independent "
                "corroboration."))

    if _AUTONOMOUS_RE.search(body or "") \
            and _operator_in_chain(graph, branch_id, findings_context):
        flags.append((
            "autonomy",
            "the draft claims autonomous/unprompted operation, but an operator "
            "message or activation is in this branch's causal chain — the work "
            "was operator-steered, not unprompted."))

    if _DEMONSTRATED_RE.search(body or "") and evidence_ids \
            and not _has_demonstration(graph, evidence_ids):
        flags.append((
            "evidence strength",
            "the draft uses verified/proven/demonstrated language, but the "
            "cited evidence is descriptive (observations), not a demonstration "
            "(an evaluation, measurement, or test) — soften to what the "
            "evidence shows."))

    superlatives = []
    for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", body or "")):
        if not para or para.startswith("#") or para.startswith("[^"):
            continue
        m = _SUPERLATIVE_RE.search(para)
        if m and not _FOOTNOTE_RE.search(para):
            superlatives.append((i + 1, m.group(0)))
    if superlatives:
        phrases = ", ".join(f'"{w}" (¶{n})' for n, w in superlatives)
        flags.append((
            "superlatives",
            f"superlative claim(s) {phrases} carry no evidence footnote — a "
            "first/only/best claim needs supporting evidence or a hedge."))

    if not flags:
        return None
    lines = ["> **Review note (overclaim lint):** the following phrasings claim "
             "more than the graph evidence supports — for operator review, not "
             "auto-blocked:"]
    for label, detail in flags:
        lines.append(f"> - _{label}_: {detail}")
    return "\n\n" + "\n".join(lines)


@llm_behavior(
    name="draft_writer",
    on=["object.created"],
    where={"object.type": "observation", "object.data.metadata.draft_request": True},
    description=_file_default_description("draft_writer"),
    output_schema=BlogDraft,
    model=None,
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["artifact", "decision"],
    max_tokens=4096,
    tools=[],
)
def draft_writer(event, graph, ctx, out, *, settings: LabSettings):
    """Turn a draft request into a blog post draft + a gated publish decision.

    On: object.created (observation, metadata.draft_request=true) — the ONLY
    trigger (ADR-014). Requests come from digest (accumulated findings →
    note), gate (decided branch / synthesis → research), or the operator's
    chat escape hatch. The request's data carries the code-injected draft
    context: classification guidance and per-item provenance (4a/5a).

    Creates: artifact (kind=blog_draft, status=draft, metadata.post_kind,
    mirrored to drafts/<slug>.md — the graph copy is canonical) + produced
    relation + decision (kind=publish, status=pending). The gate does the
    rest: NOTHING publishes without an approved decision.

    Post-generation: claims-coverage check appends a review note for any
    paragraph without evidence footnotes and for first-person process claims
    without a matching evidence ref (5b); a provenance block (branch,
    evidence, event horizon, model, crawl mode) is always appended.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    obs_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}

    if not obs_id or obs_id in _DRAFTED_OBS:
        return
    _DRAFTED_OBS.add(obs_id)

    if out is None or is_inert(getattr(out, "title", None)):
        return
    body = (getattr(out, "body_markdown", None) or "").strip()
    if not body:
        return

    branch_id = meta.get("lab_branch_id")
    finding_ids = list(meta.get("finding_ids") or [])
    evidence = list(meta.get("evidence_refs") or [])
    for f in finding_ids:
        if f not in evidence:
            evidence.append(f)
    # Phase 5 (ADR-029): under an operator brief, what the body footnotes is
    # the truth of what the post rests on — the queued-findings pile in the
    # request metadata may name unrelated digest findings (artifact#868/#882
    # listed digest findings while the body cited the loop/verify entities).
    if meta.get("operator_brief"):
        footnoted = _footnoted_entity_ids(body)
        if footnoted:
            evidence = footnoted
    if not evidence:
        evidence.append(obs_id)

    post_kind = getattr(out, "post_kind", None) or meta.get("post_kind_hint") or "note"
    if post_kind not in ("note", "research", "build"):
        post_kind = "note"

    title = (out.title or "Untitled lab note").strip()
    slug = _slugify(out.slug or title)

    # Review notes append to the body (operator attention, never auto-blocking):
    # coverage/process/orphan (5b), then the overclaim lint (Phase 1, ADR-033).
    # Both scan the ORIGINAL body so neither lints the other's note text.
    review = _coverage_review(body)
    overclaim = _overclaim_review(
        body, graph, evidence, branch_id=branch_id,
        findings_context=meta.get("findings_context"))
    if review:
        body += review
    if overclaim:
        body += overclaim

    mission_meta = {}
    mission = None
    if branch_id:
        b = graph.get_object(branch_id)
        if b is not None and b.data.get("mission_id"):
            mission = graph.get_object(b.data["mission_id"])
    if mission is None:
        missions = [m for m in [graph.get_object(meta.get("mission_id"))] if m] \
            if meta.get("mission_id") else []
        mission = missions[0] if missions else None
    if mission is not None:
        mission_meta = mission.data.get("metadata") or {}

    crawl_mode = mission_meta.get("crawl_mode", "live")
    model_footer = _provenance_model_footer(meta)
    provenance = (
        "\n\n---\n"
        "*Provenance:* "
        f"branch `{branch_id or 'mission-level'}` · "
        f"evidence {', '.join(f'`{e}`' for e in evidence)} · "
        f"as of event `{event.id}` · "
        f"model `{model_footer}` · "
        f"crawl `{crawl_mode}`"
        + ("\n\n*Note: this run crawled a synthetic snapshot, not the live "
           "site — treat site claims accordingly.*" if crawl_mode == "synthetic" else "")
    )
    content = f"# {title}\n\n{body}{provenance}\n"

    artifact = graph.add_object("artifact", {
        "kind": "blog_draft",
        "title": title,
        "content": content,
        "format": "markdown",
        "status": "draft",
        "observation_ids": evidence,
        "metadata": {"lab": "blog_draft", "slug": slug,
                     "post_kind": post_kind,
                     "lab_branch_id": branch_id,
                     "finding_id": (finding_ids[0] if finding_ids else obs_id),
                     "finding_ids": finding_ids,
                     "request_id": obs_id,
                     "seam_versions": seam_versions_stamp(graph, "prompt.draft_writer",
                                                          "charter.mission")},
    })
    if branch_id:
        graph.add_relation(branch_id, artifact.id, "produced")

    # Mirror for easy reading; failure is recorded, never fatal.
    try:
        path = _mirror_path(settings, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    except OSError as exc:
        graph.add_object("observation", {
            "text": f"Draft mirror write failed for {slug}: {exc}",
            "confidence": 1.0, "category": "risk",
            "metadata": {"lab": "draft_mirror_failure", "artifact_id": artifact.id},
        })

    graph.add_object("decision", {
        "subject_ref": artifact.id,
        "kind": "publish",
        "status": "pending",
        "rationale": f"Publish blog draft '{title}' ({slug}.md).",
        "evidence_refs": evidence,
        "metadata": {"requested_by": "lab.draft_writer", "lab_branch_id": branch_id},
    })


# ---------------------------------------------------------------- seam_writer
# Phase 4 rails: the LLM stage of a chat-triggered seam proposal. The
# steering intent (in answer) assembles the request — current version
# verbatim + cited evidence — and this behavior authors the next version,
# which propose_seam_fn turns into a seam artifact + pending self_modify
# decision through the EXISTING gate. Nothing auto-applies; the reserved
# prompt.draft_writer voice episode stays reserved (these are the rails,
# not the performance).


@llm_behavior(
    name="seam_writer",
    on=["object.created"],
    where={"object.type": "observation",
           "object.data.metadata.lab": "seam_proposal_request"},
    description=_PROMPTS["seam_writer"],
    output_schema=SeamProposal,
    model=None,
    view={"around": "event.payload.object.id", "depth": 1, "recent_events": 0},
    creates=["artifact", "decision", "observation", "comm_response_candidate"],
    max_tokens=4096,
    tools=[],
)
def seam_writer(event, graph, ctx, out, *, settings: LabSettings):
    """Author the requested seam version and open the gated proposal.

    Creates: artifact (kind=seam, next version) + decision (self_modify,
    pending) via propose_seam_fn — the decision's evidence_refs carry the
    ids that informed the proposal (the operator's message, the rejected
    decisions), so the proposal event records them.

    VERBATIM enforcement (decision#195): text the operator marked VERBATIM
    must appear intact in the generated body (substring after whitespace
    normalization). A body that drops or alters it never becomes a
    proposal — a seam_proposal_failed observation records the diff and a
    reply in the branch thread says so.
    """
    consume_llm_anomalies(graph)
    obj = event.payload.get("object", {})
    obs_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    seam_name = meta.get("seam_name")
    if not obs_id or obs_id in _SEAM_PROPOSED or not seam_name:
        return
    _SEAM_PROPOSED.add(obs_id)

    from .seams import propose_seam_fn
    body = (getattr(out, "body", None) or "").strip() if out is not None else ""
    if not body or is_inert(getattr(out, "rationale", None)):
        graph.add_object("observation", {
            "text": (f"Seam proposal for {seam_name} could not be authored "
                     "(LLM budget, pause, or parse failure) — the request "
                     "stands on the record; ask again to retry."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "seam_proposal_failed", "seam_name": seam_name,
                         "request_id": obs_id},
        })
        return
    diff = _verbatim_diff(body, list(meta.get("verbatim_sections") or []))
    if diff:
        first = diff[0]
        graph.add_object("observation", {
            "text": (f"Seam proposal for {seam_name} BLOCKED: the generated "
                     "body dropped or altered operator-marked VERBATIM text "
                     f"(kept {first['matched_chars']} of "
                     f"{first['expected_chars']} normalized chars). No "
                     "proposal was opened; the diff is on this observation."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "seam_proposal_failed", "seam_name": seam_name,
                         "request_id": obs_id, "verbatim_diff": diff,
                         "lab_branch_id": meta.get("lab_branch_id")},
        })
        reply = (f"The seam proposal for {seam_name} was NOT opened: the "
                 "generated body dropped or altered the text you marked "
                 f"VERBATIM (kept {first['matched_chars']} of "
                 f"{first['expected_chars']} chars after whitespace "
                 "normalization). The diff is recorded as a "
                 "seam_proposal_failed observation — re-send the request to "
                 "retry.\n\n— as of event " + str(event.id))
        candidate = graph.add_object("comm_response_candidate", {
            "message_id": meta.get("message_id"),
            "thread_id": meta.get("thread_id"),
            "channel": settings.answer_channel,
            "content": reply,
            "status": ("approved" if settings.auto_approve_answers
                       else "proposed"),
            "created_by_behavior": "lab.seam_writer",
            "metadata": {"event_horizon": str(event.id),
                         "lab": "seam_proposal_failed_reply",
                         "request_id": obs_id,
                         "provenance": {"branch_id": meta.get("lab_branch_id")}},
        })
        if meta.get("message_id"):
            graph.add_relation(candidate.id, meta["message_id"], "response_to")
        return
    rationale = (getattr(out, "rationale", None) or "").strip() \
        or f"Operator-requested revision of {seam_name}."
    propose_seam_fn(graph, seam_name, body, rationale,
                    evidence_refs=list(meta.get("evidence_refs") or []),
                    request_id=obs_id,
                    requested_by="lab.seam_writer")


# ---------------------------------------------------------------- answer

_STEER_PAUSE = ("pause",)
_STEER_RESUME = ("resume", "unpause", "reactivate")
_STEER_APPROVE = ("approve",)
_STEER_REJECT = ("reject",)
_STEER_DRAFT = ("draft",)
_STEER_PROPOSE = ("propose",)
_STEER_ACTIVATE = ("activate",)
_STEER_DEACTIVATE = ("deactivate",)
_STEER_RECRAWL = ("recrawl", "re-crawl")
# ADR-028: commentary, not control — records the operator's message as a
# branch observation. The one steering verb that claims nothing and changes
# nothing; MCP-allowed (it is not authority over the branch).
_STEER_NOTE = ("note", "comment")

# The verb set a refusal names (ADR-025). Order = documentation order.
SUPPORTED_STEERING_VERBS = (
    "pause", "resume", "activate", "deactivate", "draft", "approve",
    "reject", "recrawl", "note", "propose <seam>")

# Action-shaped requests no steering verb supports. When one of these appears
# and no supported verb matched, the reply is an explicit refusal naming the
# verb set — never a narration of an action that did not happen (ADR-025;
# the evt_3676 incident: "Activating this branch now... task dispatched"
# replied to a verb that did not exist).
_UNSUPPORTED_ACTION_RE = re.compile(
    r"\b(archive|delete|remove|publish|post|merge|deploy|restart|reboot|"
    r"cancel|abort|stop|kill|terminate|promote|demote|abandon|rename|"
    r"escalate|fork|dispatch|execute)\b")


def _has_verb(low: str, words: tuple[str, ...]) -> bool:
    """Word-boundary verb match. Substring matching mis-fired in production:
    'activate' lives inside 'deactivate'/'reactivate' and 'pause' inside
    'unpause' — boundaries make each verb match only itself."""
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)


def _unsupported_action(low: str) -> Optional[str]:
    m = _UNSUPPORTED_ACTION_RE.search(low)
    return m.group(1) if m else None


def _steering_event(graph, verb: str, branch_id: str, msg_id: str,
                    summary: str, source: Optional[str],
                    refs: Optional[dict] = None) -> Optional[str]:
    """Append the lab.steering_applied marker (marker-event family,
    ADR-013/015/021/025) recording WHAT a chat verb mutated, and return its
    event id — the citation the reply carries. The reply may only claim
    actions it can cite (ADR-025)."""
    try:
        ev = graph.emit("lab.steering_applied", {
            "verb": verb, "branch_id": branch_id, "message_id": msg_id,
            "summary": summary, "source": source or "operator",
            "refs": dict(refs or {}),
        })
        return str(getattr(ev, "id", "")) or None
    except Exception:
        return None

# Phase 4: which seam is the operator talking about? An explicit seam name
# wins; otherwise "<behavior> … prompt" or "charter" phrasing resolves.
_SEAM_NAME_RE = re.compile(
    r"\b(prompt\.[a-z_]+|setting\.[a-z_.]+|charter\.mission|"
    r"template\.feed\.[a-z_]+)\b")
_SEAM_PROMPT_BEHAVIORS = ("draft_writer", "research_worker", "interpret",
                          "plan", "answer")


def _seam_name_from_message(low: str) -> Optional[str]:
    m = _SEAM_NAME_RE.search(low)
    if m:
        return m.group(1)
    if "prompt" in low:
        for b in _SEAM_PROMPT_BEHAVIORS:
            if b in low:
                return f"prompt.{b}"
    if "charter" in low:
        return "charter.mission"
    return None


def _current_seam_body(graph, seam_name: str) -> tuple[int, str]:
    """The version + body in force for a seam — graph override else file
    default — so the proposal's LLM context contains what it would replace."""
    from .seams import active_charter, resolve
    if seam_name == "charter.mission":
        return active_charter(graph)
    if seam_name.startswith("prompt."):
        default = _PROMPTS.get(seam_name.split(".", 1)[1], "")
        return resolve(graph, seam_name, default)
    version, body = resolve(graph, seam_name, None)
    return version, (body if body is not None else "")


# Operator-marked verbatim text (the decision#195 truncation): everything
# from a VERBATIM marker to END VERBATIM, or to the end of the message.
# Uppercase only — the marker is a deliberate act, not the word in prose.
# The END marker is CONSUMED (not a lookahead), so the scan never re-matches
# the word inside "END VERBATIM" and captures trailing prose as a section.
_VERBATIM_BLOCK_RE = re.compile(
    r"\bVERBATIM\b:?[ \t]*\n?(.+?)(?:\bEND[ \t]+VERBATIM\b|\Z)", re.S)


def _verbatim_sections(content: str) -> list[str]:
    return [m.strip() for m in _VERBATIM_BLOCK_RE.findall(content or "")
            if m.strip()]


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def _verbatim_diff(body: str, sections: list[str]) -> list[dict]:
    """VERBATIM sections the generated body dropped or altered: each entry
    records how far (whitespace-normalized) the body got into the marked
    text and the tail it lost — the diff the failure observation carries."""
    nb = _normalize_ws(body)
    failures = []
    for s in sections:
        ns = _normalize_ws(s)
        if not ns or ns in nb:
            continue
        lo, hi = 0, len(ns)  # longest prefix of ns still present in the body
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ns[:mid] in nb:
                lo = mid
            else:
                hi = mid - 1
        failures.append({"expected_chars": len(ns), "matched_chars": lo,
                         "expected_head": ns[:120],
                         "missing_text": ns[lo:]})
    return failures


def _rejected_for_seam(seam_name: str) -> list[dict]:
    """Rejected decisions a proposal for `seam_name` may cite (decision#195's
    second defect: a charter proposal cited publish rejections). Publish
    rejections speak to the draft_writer's voice; a rejected decision on the
    SAME seam speaks to that seam; everything else is noise here."""
    relevant = [d for d in _REJECTED_DECISIONS
                if d.get("seam_name") == seam_name
                or (seam_name == "prompt.draft_writer"
                    and d.get("kind") == "publish")]
    return relevant[-6:]


def _request_seam_proposal(graph, branch_id: str, msg_id: str,
                           content: str, seam_name: str) -> str:
    """Phase 4 rails: assemble the seam-proposal request the seam_writer
    behavior drafts from — current version verbatim plus the cited evidence
    (the operator's message and the seam-relevant rejected decisions on the
    record). The proposal itself goes through propose_seam_fn → the EXISTING
    gate; nothing auto-applies."""
    version, body = _current_seam_body(graph, seam_name)
    evidence = [msg_id]
    rejected = []
    for d in _rejected_for_seam(seam_name):
        evidence.append(d["id"])
        item = {"decision_id": d["id"], "kind": d["kind"],
                "subject_ref": d["subject_ref"]}
        if d.get("resolution_rationale"):
            # ADR-026: the operator's stated reason for the rejection — the
            # evidence a proposal should actually argue from.
            item["resolution_rationale"] = d["resolution_rationale"]
        subject = graph.get_object(d["subject_ref"]) if d["subject_ref"] else None
        if subject is not None:
            item["subject_title"] = subject.data.get("title")
            if d["subject_ref"] not in evidence:
                evidence.append(d["subject_ref"])
        rejected.append(item)
    verbatim = _verbatim_sections(content)
    msg = graph.get_object(msg_id) if msg_id else None
    text = (f"Seam proposal requested via chat: author the next version "
            f"of {seam_name} (current v{version}), addressing the cited "
            "evidence. The proposal opens a pending self_modify decision "
            "— nothing applies without the gate.")
    if verbatim:
        text += (f" The operator marked {len(verbatim)} section(s) VERBATIM: "
                 "they must appear in the proposed body without paraphrase.")
    graph.add_object("observation", {
        "text": text,
        "confidence": 1.0,
        "category": "fact",
        # The operator's message and the current body ride IN FULL — the
        # seam_writer's view serializes this object whole, so an excerpt
        # here IS a truncation in the proposal (decision#195/artifact#194).
        "metadata": {"lab": "seam_proposal_request",
                     "seam_name": seam_name,
                     "current_version": version,
                     "current_body": body or "",
                     "operator_request": content,
                     "verbatim_sections": verbatim,
                     "evidence_refs": evidence,
                     "rejected_decisions": rejected,
                     "lab_branch_id": branch_id,
                     "thread_id": (msg.data.get("thread_id")
                                   if msg is not None else None),
                     "message_id": msg_id},
    })
    return f"seam proposal requested for {seam_name} (gated, pending review)"


def _pending_for_branch(graph, branch_id: str) -> list:
    """Pending decisions keyed by branch (ADR-025): promote decisions whose
    subject IS the branch plus publish/self_modify decisions tagged with
    metadata.lab_branch_id — verified still pending against the graph (the
    registry is a cache, never the truth)."""
    out = []
    for d_id in _PENDING_BY_BRANCH.get(branch_id, []):
        d = graph.get_object(d_id)
        if d is not None and d.data.get("status") == "pending":
            out.append(d)
    return out


def _applied(verb, summary, event_id):
    return {"kind": "applied", "verb": verb, "summary": summary,
            "event_id": event_id}


def _noop(verb, summary):
    return {"kind": "noop", "verb": verb, "summary": summary,
            "event_id": None}


def _resolve_decision_verb(graph, branch_id: str, msg_id: str, verb: str,
                           source: Optional[str]) -> dict:
    """Chat approve/reject (ADR-025). MCP-tagged messages are REFUSED — the
    inbox is human-only (ADR-016/021); the old subject_ref keying bug was a
    side door that failed closed, and it stays closed. Exactly one pending
    decision applies; multiple list ids without mutating; zero is an honest
    no-op."""
    if source == "operator_via_mcp":
        return {"kind": "refused", "verb": verb, "event_id": None,
                "summary": (
                    f"I did not {verb} anything: decisions cannot be resolved "
                    "from this surface. Approve/reject authority stays with "
                    "the human operator (ADR-016/021) — messages relayed over "
                    "MCP (source=operator_via_mcp) are refused for decision "
                    "verbs. Use the inbox at /lab.")}
    pending = _pending_for_branch(graph, branch_id)
    if not pending:
        return _noop(verb, f"Nothing to {verb}: no pending decision "
                           "references this branch.")
    if len(pending) > 1:
        listed = "; ".join(
            f"{d.id} ({d.data.get('kind')} on {d.data.get('subject_ref')})"
            for d in pending)
        return _noop(verb, (
            f"Nothing was {verb}d: {len(pending)} decisions are pending on "
            f"this branch — {listed}. Resolve a specific one from the inbox "
            "at /lab."))
    decision = pending[0]
    new_status = "approved" if verb == "approve" else "rejected"
    # Resolve through the one resolution path (ADR-026): resolved_by stamps,
    # pending annotations link into the evidence. The operator's chat message
    # IS the rationale here — it is already in the log; record it on the
    # resolution event too so the registry carries the reason.
    from .tools import approve_decision_fn
    msg = graph.get_object(msg_id) if msg_id else None
    rationale = (msg.data.get("content") or "")[:400] if msg is not None else ""
    approve_decision_fn(graph, decision.id, verb == "approve", rationale)
    summary = (f"decision {decision.id} ({decision.data.get('kind')} on "
               f"{decision.data.get('subject_ref')}) {new_status}")
    event_id = _steering_event(graph, verb, branch_id, msg_id, summary,
                               source, refs={"decision_id": decision.id})
    return _applied(verb, summary, event_id)


def _apply_steering(graph, branch_id: str, content: str,
                    msg_id: Optional[str] = None,
                    source: Optional[str] = None) -> Optional[dict]:
    """Deterministic steering: the effect lands at this event boundary and
    the reply is composed AFTER it, from post-mutation graph state
    (ADR-025). Returns None when no steering verb matched, else a result
    dict: kind=applied (with the lab.steering_applied event id the reply
    cites), noop, or refused — each with a reply-ready summary."""
    low = content.lower()
    msg_id = msg_id or ""
    branch = graph.get_object(branch_id)
    if branch is None:
        return None
    status = branch.data.get("status")

    # ADR-027: an archived branch is chat-able for ONE steering verb —
    # activate (deliberate operator resurrection, handled below). ADR-028
    # adds `note`: commentary changes nothing, so it is safe in any state.
    # Every other verb or action request draws an honest refusal naming it;
    # plain questions fall through to answer, which states the archived status.
    if status == "archived" and not _has_verb(low, _STEER_ACTIVATE) \
            and not _has_verb(low, _STEER_NOTE):
        other_verbs = (_STEER_PAUSE + _STEER_RESUME + _STEER_DEACTIVATE
                       + _STEER_RECRAWL + _STEER_DRAFT + _STEER_APPROVE
                       + _STEER_REJECT + _STEER_PROPOSE)
        if _has_verb(low, other_verbs) or _unsupported_action(low):
            return {"kind": "refused", "verb": "archived", "event_id": None,
                    "summary": (
                        "This branch is archived. The only steering verb an "
                        "archived branch accepts is 'activate' — a deliberate "
                        "operator resurrection, recorded as such. Nothing was "
                        "changed.")}
        return None

    # Checked before the draft verb — "propose an improved draft_writer
    # prompt" contains 'draft' but is a proposal request.
    if _has_verb(low, _STEER_PROPOSE):
        seam_name = _seam_name_from_message(low)
        if seam_name:
            summary = _request_seam_proposal(graph, branch_id, msg_id,
                                             content, seam_name)
            event_id = _steering_event(graph, "propose", branch_id, msg_id,
                                       summary, source,
                                       refs={"seam_name": seam_name})
            return _applied("propose", summary, event_id)
    if _has_verb(low, _STEER_PAUSE):
        if status == "paused":
            return _noop("pause", "This branch is already paused.")
        graph.patch_object(branch_id, {"status": "paused"})
        event_id = _steering_event(graph, "pause", branch_id, msg_id,
                                   "branch paused", source)
        return _applied("pause", "branch paused", event_id)
    if _has_verb(low, _STEER_RESUME):
        if status != "paused":
            return _noop("resume", f"This branch is not paused "
                                   f"(status={status}) — nothing to resume.")
        graph.patch_object(branch_id, {"status": "active"})
        event_id = _steering_event(graph, "resume", branch_id, msg_id,
                                   "branch resumed (status=active)", source)
        return _applied("resume", "branch resumed (status=active)", event_id)
    if _has_verb(low, _STEER_DEACTIVATE):
        # ADR-025: the reversible twin of activate — back to proposed.
        if status != "active":
            return _noop("deactivate",
                         f"This branch is not active (status={status}) — "
                         "deactivate only reverts an active branch to "
                         "proposed.")
        graph.patch_object(branch_id, {"status": "proposed"})
        event_id = _steering_event(graph, "deactivate", branch_id, msg_id,
                                   "branch deactivated (status=proposed)",
                                   source)
        return _applied("deactivate", "branch deactivated (status=proposed)",
                        event_id)
    if _has_verb(low, _STEER_ACTIVATE):
        # ADR-025: operator authority, MCP-allowed — reversible like pause
        # (ADR-021's argument). Activation records the operator's rationale
        # as an observation and lets the EXISTING dispatch react to the
        # status patch (work fires on status→active; nothing new dispatches).
        # ADR-027 widens the verb: decided → active is a continuation (the
        # rejected-promote path leaves direction on the branch); archived →
        # active is a deliberate operator resurrection, recorded as such.
        if status == "active":
            return _noop("activate", "This branch is already active.")
        if status == "paused":
            return _noop("activate", "This branch is paused — use resume.")
        if status not in ("proposed", "scoped", "decided", "archived"):
            return _noop("activate",
                         f"This branch is {status} — only a proposed, "
                         "scoped, decided, or archived branch can be "
                         "activated.")
        resurrected = status == "archived"
        obs = graph.add_object("observation", {
            "text": ((f"Branch resurrected from archived by the operator "
                      f"(deliberate resurrection). Rationale: {content[:400]}")
                     if resurrected else
                     (f"Branch activated by the operator"
                      + (f" (from {status})" if status == "decided" else "")
                      + f". Rationale: {content[:400]}")),
            "confidence": 1.0,
            "category": "fact",
            "metadata": {"lab": "branch_activated", "lab_branch_id": branch_id,
                         "previous_status": status,
                         "resurrected": resurrected,
                         "message_id": msg_id, "source": source or "operator"},
        })
        graph.add_relation(branch_id, obs.id, "supported_by")
        # A deliberate operator activation is a fresh dispatch episode: the
        # dedup registry resets for this branch (the recrawl move — the
        # registry is a cache, the log is append-only) so work dispatches a
        # NEW task, which carries any operator_direction on the branch.
        # The activation message rides on the branch metadata too: URLs the
        # operator mentions at activation steer the worker's sources (the
        # existing steering principle, extended to this verb — decision#266's
        # direction named its sources without schemes, so the resurrection
        # message must be able to supply fetchable ones).
        _DISPATCHED.discard(branch_id)
        graph.patch_object(branch_id, {
            "status": "active",
            "metadata": {**(branch.data.get("metadata") or {}),
                         "activation_message": content},
        })
        summary = (("branch resurrected from archived (status=active); "
                    "dispatch reacts next") if resurrected else
                   f"branch activated from {status} (status=active); "
                   "dispatch reacts next")
        directions = _OPERATOR_DIRECTIONS.get(branch_id) or []
        if directions:
            summary += " — the operator direction on record rides with the task"
        event_id = _steering_event(graph, "activate", branch_id, msg_id,
                                   summary, source,
                                   refs={"rationale_observation": obs.id,
                                         "previous_status": status})
        return _applied("activate", summary, event_id)
    if _has_verb(low, _STEER_RECRAWL):
        # ADR-025/crawl fix: replay never re-fires behaviors, so a resumed
        # lab whose crawl stalled needs an operator nudge. The request is a
        # crawl_request source ingest ALREADY reacts to, scoped to the
        # mission target_url; the in-process crawl registry resets so dedup
        # does not no-op a deliberate re-fetch (the registry is a cache —
        # existing observations are never mutated, the log is append-only).
        mission = graph.get_object(branch.data.get("mission_id")) \
            if branch.data.get("mission_id") else None
        target = (mission.data.get("target_url") or "").strip() \
            if mission is not None else ""
        if not target:
            return _noop("recrawl", "No mission target_url is linked to "
                                    "this branch — nothing to recrawl.")
        _CRAWLS[mission.id] = {"visited": set(), "fetched": 0, "queued": set()}
        req = graph.add_object("source", {
            "kind": "crawl_request",
            "content": target,
            "url": target,
            "channel": "lab",
            "metadata": {"mission_id": mission.id, "depth": 0,
                         "requested_by": "operator", "message_id": msg_id},
        })
        summary = f"recrawl requested for {target} (fresh crawl episode)"
        event_id = _steering_event(graph, "recrawl", branch_id, msg_id,
                                   summary, source,
                                   refs={"crawl_request": req.id,
                                         "mission_id": mission.id})
        return _applied("recrawl", summary, event_id)
    if _has_verb(low, _STEER_DRAFT):
        # 4b escape hatch: the operator can request a draft on anything.
        # Explicit attention — bypasses the pending-publish cap (ADR-014).
        # The full message rides as the OPERATOR BRIEF (the evt_13857
        # compression: only this 160-char rationale used to survive).
        evidence = _branch_evidence_ids(graph, branch_id)
        hint = "research" if status == "decided" else "note"
        req = _request_draft(
            graph, post_kind_hint=hint, item_ids=evidence,
            requested_by="operator", branch_id=branch_id,
            mission_id=branch.data.get("mission_id"),
            rationale=(f"Operator requested a draft on branch "
                       f"'{branch.data.get('title')}': {content[:160]}"),
            operator_brief=content,
        )
        summary = (f"draft requested on this branch ({hint}; operator "
                   "escape hatch)")
        event_id = _steering_event(graph, "draft", branch_id, msg_id,
                                   summary, source,
                                   refs={"draft_request": req.id})
        return _applied("draft", summary, event_id)
    if _has_verb(low, _STEER_APPROVE):
        return _resolve_decision_verb(graph, branch_id, msg_id, "approve",
                                      source)
    if _has_verb(low, _STEER_REJECT):
        return _resolve_decision_verb(graph, branch_id, msg_id, "reject",
                                      source)
    if _has_verb(low, _STEER_NOTE):
        # ADR-028: operator commentary — record the FULL message as a branch
        # observation (kind=operator_note), confirm with the recorded event
        # id, claim nothing else. Checked AFTER every command verb so a real
        # command ("pause this branch") still wins; this is the fallback for
        # commentary that is not a command (the evt_17441 erratum, refused
        # because no verb matched). Status is never touched. MCP-allowed.
        obs = graph.add_object("observation", {
            "text": content,
            "confidence": 1.0,
            "category": "fact",
            "metadata": {"lab": "operator_note", "kind": "operator_note",
                         "lab_branch_id": branch_id, "message_id": msg_id,
                         "source": source or "operator"},
        })
        graph.add_relation(branch_id, obs.id, "supported_by")
        summary = f"operator note recorded as observation {obs.id}"
        event_id = _steering_event(graph, "note", branch_id, msg_id, summary,
                                   source, refs={"note_observation": obs.id})
        return _applied("note", summary, event_id)
    return None


@llm_behavior(
    name="answer",
    on=["object.created"],
    # INVARIANT (ADR-016): this predicate matches the channel, never the
    # message's metadata.source provenance tag. Operator authority is the
    # server-stamped sender (a valid token IS the operator); source is
    # display provenance — operator, operator_via_mcp, and any future
    # operator_via_* tag must all draw a reply. Narrowing this `where` to a
    # literal source tag silently orphans every other operator surface
    # (locked by the thread_equals_branch fixture and scripts/test_mcp.py).
    where={
        "object.type": "comm_message",
        "object.data.channel": "lab",
        "object.data.direction": "inbound",
    },
    description=_PROMPTS["answer"],
    output_schema=AnswerReply,
    model=None,
    view={
        "around": "event.payload.object.data.metadata.lab_branch_id",
        "depth": 1,
        "recent_events": 0,
    },
    creates=["comm_response_candidate", "observation", "source"],
    max_tokens=1024,
    tools=[],
)
def answer(event, graph, ctx, out, *, settings: LabSettings):
    """Reply inside a branch thread from graph state, stamped with its horizon.

    On: object.created (comm_message, channel=lab, direction=inbound). The lab
    channel is scoped in `where` so the (potentially paid) LLM call fires only
    for branch-thread messages — the same precision-scoping the upstream chat
    pack uses. Communication's intent_detector still records a comm_intent for
    the message; this behavior keys off the message itself.

    Creates: comm_response_candidate with an event-horizon stamp ("as of event
    N" = the triggering event — nothing later was visible) and provenance refs.
    Never blocks on running work: it reads only committed graph state.

    Truthful steering (ADR-025): steering mutations apply FIRST, and for any
    action-shaped message the reply is composed deterministically from
    POST-MUTATION graph state — an applied verb cites its
    lab.steering_applied event id, a no-op/refusal says so, and an action no
    verb supports draws an explicit refusal naming the verb set. The model's
    pre-mutation narration is never used for action messages: it cannot cite
    a mutation, so it may not claim one (the evt_3676 incident — "Activating
    this branch now... task dispatched" — narrated a verb that did not
    exist).
    """
    obj = event.payload.get("object", {})
    msg_id = obj.get("id")
    data = obj.get("data", {})
    meta = data.get("metadata") or {}
    branch_id = meta.get("lab_branch_id")
    thread_id = data.get("thread_id") or meta.get("thread_id_hint")

    if not branch_id and thread_id:
        branch_id = _THREAD_TO_BRANCH.get(thread_id)
    if not branch_id:
        return  # Not a branch thread — the generic chat pack may still reply.

    consume_llm_anomalies(graph)
    content = data.get("content") or ""
    result = _apply_steering(graph, branch_id, content, msg_id,
                             source=meta.get("source"))

    # Post-mutation state: read AFTER steering applied (ADR-025).
    branch = graph.get_object(branch_id)
    b_status = branch.data.get("status") if branch is not None else None
    status_line = (f"Branch “{branch.data.get('title')}” is now {b_status}."
                   if branch is not None else "")

    unsupported = None if result else _unsupported_action(content.lower())
    if result is not None:
        if result["kind"] == "applied" and result.get("event_id"):
            reply = (f"Applied: {result['summary']} — recorded at "
                     f"{result['event_id']}.")
        elif result["kind"] == "applied":
            reply = f"Applied: {result['summary']}."
        else:
            reply = result["summary"]
        reply += f"\n\n{status_line}"
    elif unsupported:
        reply = (f"I can't do that: no steering verb performs '{unsupported}' "
                 "from chat, and I won't claim actions I can't cite. "
                 f"Supported verbs in a branch thread: "
                 f"{', '.join(SUPPORTED_STEERING_VERBS)}. "
                 f"{status_line}")
    else:
        reply = (getattr(out, "reply", None) or "").strip() \
            if out is not None else ""
        if not reply or is_inert(reply):
            # Budget or parse trouble — still answer honestly from graph state.
            reply = ("I couldn't produce a model-written reply (LLM budget or "
                     "output-parse issue — recorded as an observation). ")
            if branch is not None:
                reply += (f"Branch “{branch.data.get('title')}” is currently "
                          f"{b_status}.")
        if b_status == "archived":
            # ADR-027: honesty about the archive, deterministically — the
            # model's narration must not paper over a branch that will not
            # react to anything but resurrection.
            reply = ("Note: this branch is archived — it accepts no steering "
                     "except 'activate' (operator resurrection).\n\n" + reply)
    reply += f"\n\n— as of event {event.id}"

    candidate = graph.add_object("comm_response_candidate", {
        "message_id": msg_id,
        "thread_id": thread_id,
        "channel": data.get("channel") or settings.answer_channel,
        "content": reply,
        "status": "approved" if settings.auto_approve_answers else "proposed",
        "created_by_behavior": "lab.answer",
        "metadata": {
            "event_horizon": str(event.id),
            "seam_versions": seam_versions_stamp(graph, "prompt.answer"),
            "steering": ({"verb": result["verb"], "kind": result["kind"],
                          "event_id": result.get("event_id")}
                         if result else None),
            "provenance": {
                "branch_id": branch_id,
                "mission_id": (branch.data.get("mission_id") if branch else None),
                "branch_status": b_status,
            },
        },
    })
    graph.add_relation(candidate.id, msg_id, "response_to")


# Registration order is execution order within an event batch. The research
# worker stages (ADR-020) live in lab_pack/research_worker.py — droppable
# plumbing; this import is the only registration point. It must come last:
# research_worker.py imports nothing from this module at import time.
from .research_worker import research_intake, research_worker  # noqa: E402

BEHAVIORS = [ingest, plan, work, research_intake, interpret, digest, gate,
             draft_writer, research_worker, seam_writer, answer]
