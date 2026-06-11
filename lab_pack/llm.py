"""Lab pack — LLM output schemas and provider selection.

Mirrors packs/chat/llm.py from activegraph-packs: structured output schemas
for each llm_behavior, a deterministic mock provider for the no-API-key path
(fixtures MUST run without a key), and an environment-driven selector.

SECURITY: API keys are read from the environment by the native providers at
call time. They never enter the graph, events, logs, or artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from activegraph.llm import AnthropicProvider, LLMResponse, OpenAIProvider

from .kernel import ABSOLUTE_DAILY_COST_CEILING_USD


# ---------------------------------------------------------------- schemas


class PlanProposal(BaseModel):
    """Structured output for the plan behavior."""

    should_branch: bool = Field(
        description="True if this claim's evidence gap warrants a new branch of inquiry."
    )
    title: str = Field(default="", description="Short branch title (5-10 words).")
    intent: str = Field(
        default="",
        description="What the branch should find out or produce, one or two sentences.",
    )
    reasoning: str = Field(
        default="",
        description="Narrated prioritization: why this gap, why now. Never a formula score.",
    )


class InterpretSummary(BaseModel):
    """Structured output for the interpret behavior."""

    summary: str = Field(description="What the completed/failed work showed, 2-4 sentences.")
    outcome: Literal["decided", "follow_up"] = Field(
        default="decided",
        description="'decided' to close the branch question, 'follow_up' to propose a child branch.",
    )
    follow_up_intent: str = Field(
        default="",
        description="Intent for the follow-up branch when outcome='follow_up'.",
    )


class AnswerReply(BaseModel):
    """Structured output for the answer behavior."""

    reply: str = Field(description="The reply to the user's message, grounded in graph state.")


class ResearchFinding(BaseModel):
    """One source-attributed finding from the research worker (ADR-020)."""

    text: str = Field(description="The finding, one or two plain sentences.")
    source_urls: list[str] = Field(
        default_factory=list,
        description=(
            "The fetched source URL(s) this finding rests on. A finding "
            "with no valid fetched-source attribution is dropped."
        ),
    )


class ResearchSynthesis(BaseModel):
    """Structured output for the research_worker behavior (ADR-020)."""

    summary: str = Field(
        description="2-4 sentences: what the sources collectively show, "
                    "including what they fail to show.")
    findings: list[ResearchFinding] = Field(
        default_factory=list,
        description="1-5 source-attributed findings.")


class BlogDraft(BaseModel):
    """Structured output for the draft_writer behavior."""

    title: str = Field(description="Post title, plain and specific. No hype.")
    slug: str = Field(
        default="",
        description="URL slug (lowercase, hyphens). Derived from the title if empty.",
    )
    post_kind: Literal["note", "research", "build"] = Field(
        default="note",
        description=(
            "Editorial kind (ADR-014). The draft request in the view carries "
            "code-injected classification guidance and a suggested kind; "
            "follow it unless the content clearly says otherwise."
        ),
    )
    body_markdown: str = Field(
        description=(
            "The post body in markdown, 400-900 words, per the draft contract: "
            "every factual claim cites evidence by object/event id as a "
            "footnote; structure = what we tried / what happened / what it "
            "means / what's next; first person singular; failures are findings."
        ),
    )


# ---------------------------------------------------------------- mock provider


def _extract_field(messages: list, field: str) -> str:
    """Pull a JSON string field (e.g. the triggering observation's text) out of
    the serialized prompt. Best-effort; used only to make mock output distinct."""
    blob = " ".join(str(getattr(m, "content", m)) for m in messages)
    found = re.findall(r'"%s":\s*"([^"]{10,600}?)"' % field, blob)
    return found[-1] if found else ""


class LabMockProvider:
    """Deterministic scripted LLMProvider for fixtures and keyless runs.

    Output depends only on the requested output_schema and the prompt text,
    so fixture runs are reproducible without an API key.
    """

    default_model: str = "mock-lab-1"

    def complete(
        self,
        *,
        system: str,
        messages: list,
        model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        output_schema: Optional[type],
        timeout_seconds: float,
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> LLMResponse:
        name = getattr(output_schema, "__name__", "")
        digest = hashlib.sha256(
            " ".join(str(getattr(m, "content", m)) for m in messages).encode()
        ).hexdigest()[:8]

        if name == "PlanProposal":
            claim = _extract_field(messages, "text") or f"claim {digest}"
            short = claim[:60].rstrip(". ")
            parsed: Any = PlanProposal(
                should_branch=True,
                title=f"Verify: {short}",
                intent=f"Find or produce evidence that tests the claim: '{claim[:200]}'.",
                reasoning=(
                    f"The site asserts '{short}' but the graph holds no linked "
                    "evidence object for it yet. Verifying a published claim with "
                    "no evidence is the mission's most direct move. [mock]"
                ),
            )
        elif name == "InterpretSummary":
            parsed = InterpretSummary(
                summary=(
                    "The dispatched work completed and its output is linked as "
                    "evidence under this branch. The branch question can be "
                    f"resolved on that basis. [mock {digest}]"
                ),
                outcome="decided",
            )
        elif name == "AnswerReply":
            asked = _extract_field(messages, "content")
            parsed = AnswerReply(
                reply=(
                    "Mock answer (no API key configured): this reply was assembled "
                    "from current graph state for the branch this thread discusses"
                    + (f", in response to: '{asked[:120]}'" if asked else "")
                    + ". Set OPENAI_API_KEY or ANTHROPIC_API_KEY for live answers."
                ),
            )
        elif name == "ResearchSynthesis":
            blob = " ".join(str(getattr(m, "content", m)) for m in messages)
            urls = []
            for u in re.findall(r'"url":\s*"(https?://[^"]+)"', blob):
                if u not in urls:
                    urls.append(u)
            urls = urls[:3]
            parsed = ResearchSynthesis(
                summary=(f"Synthesized {len(urls)} fetched source(s) for the "
                         f"dispatched research task; each finding below cites "
                         f"the source it rests on. [mock {digest}]"),
                findings=[ResearchFinding(
                    text=(f"The page at {u} addresses the claim under "
                          f"investigation; its excerpt is recorded as the "
                          f"linked source. [mock {digest}]"),
                    source_urls=[u]) for u in urls],
            )
        elif name == "BlogDraft":
            blob = " ".join(str(getattr(m, "content", m)) for m in messages)
            refs = re.findall(r"\b(?:observation|evaluation|task|branch)#\d+", blob)
            seen: list[str] = []
            for r_ in refs:
                if r_ not in seen:
                    seen.append(r_)
            ev = seen[:3] or [f"evidence-{digest}"]
            finding = _extract_field(messages, "text") or f"a finding ({digest})"
            footnotes = "\n".join(f"[^{i+1}]: {e}" for i, e in enumerate(ev))
            cites = "".join(f"[^{i+1}]" for i in range(len(ev)))
            hint_m = re.findall(r'"post_kind_hint":\s*"(note|research|build)"', blob)
            kind = hint_m[-1] if hint_m else "note"
            # 5c: when the injected draft context shows a SEEDED finding, the
            # mock reproduces the failure mode the coverage check must catch —
            # an invented first-person process narrative with no evidence ref.
            # Live-work findings draft clean: deterministic both ways.
            invented = ""
            if re.search(r'"origin":\s*"seeded"', blob):
                invented = ("I was reading the site late one evening and went "
                            "through the commit history by hand before writing "
                            "this up.\n\n")
            parsed = BlogDraft(
                title=f"Lab note: {finding[:60].rstrip('. ')}",
                slug=f"lab-note-{digest}",
                post_kind=kind,
                body_markdown=(
                    f"## What we tried\n\n{invented}"
                    f"I followed the mission's loop on this finding: "
                    f"{finding[:200]}{cites}\n\n"
                    f"## What happened\n\nThe runtime recorded the outcome as graph "
                    f"objects, linked below as footnotes.{cites}\n\n"
                    f"## What it means\n\nThe evidence base grew by exactly what the "
                    f"footnoted objects assert — no more.{cites}\n\n"
                    f"## What's next\n\nA follow-up branch can deepen any footnote "
                    f"that looks thin. [mock draft {digest}]\n\n{footnotes}\n"
                ),
            )
        else:
            parsed = None

        return LLMResponse(
            raw_text=json.dumps(parsed.model_dump() if parsed is not None else {"mock": True}),
            parsed=parsed,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            latency_seconds=0.0,
            model=model or self.default_model,
            finish_reason="stop",
        )

    def estimate_cost(self, *, input_tokens: int, output_tokens: int, model: str) -> Decimal:
        return Decimal("0")

    def count_tokens(self, *, system: str, messages: list, model: str) -> int:
        return 0

    def recognizes_model(self, name: str) -> bool:
        return True


# ---------------------------------------------------------------- budget + salvage

# Session-wide LLM accounting. Not graph state — graph-visible consequences
# (budget/parse observations) are recorded by the behaviors via
# consume_llm_anomalies(graph). Reset between fixtures/sessions.
_LLM_STATE: dict[str, Any] = {
    "total": 0,
    "by_behavior": {},
    "anomalies": [],          # queued {"kind", "behavior", "detail", "raw"}
    "budget_recorded": False,
    "daily_used": 0,          # authoritative via sync_daily_budget(rt)
    "daily_recorded": False,
    "daily_cost": Decimal("0"),   # today's spend, from llm.responded events
    "daily_cost_recorded": False,
    "cost_cap_override": None,    # approved setting.daily_cost_cap_usd seam
    "paused": False,              # ADR-015; rebuilt from lab.paused/resumed
    "pause_skipped": set(),       # behaviors skipped THIS pause episode
    "last_model": None,
}


def sync_daily_budget(rt) -> int:
    """7b + ADR-015: rebuild the operator-control state from the log — the
    log IS the persistence, so all of this survives restarts.

    - used-today count from llm.requested events (UTC date match; blocked
      attempts are logged BEFORE the provider runs, so they count too)
    - cost-today from the cost_usd activegraph stamps on llm.responded
    - paused from the last lab.paused / lab.resumed marker event
    - the seam-approved daily cost cap override, if any
    """
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    used = 0
    cost = Decimal("0")
    paused = _LLM_STATE["paused"]
    saw_marker = False
    for e in rt.graph.events:
        t = str(e.type)
        ts = str(getattr(e, "timestamp", "") or "")
        if t == "llm.requested" and ts.startswith(today):
            used += 1
        elif t == "llm.responded" and ts.startswith(today):
            try:
                cost += Decimal(str(e.payload.get("cost_usd") or "0"))
            except Exception:
                pass
        elif t == "lab.paused":
            paused, saw_marker = True, True
        elif t == "lab.resumed":
            paused, saw_marker = False, True
    _LLM_STATE["daily_used"] = used
    _LLM_STATE["daily_cost"] = cost
    if saw_marker and paused != _LLM_STATE["paused"]:
        _LLM_STATE["paused"] = paused
        _LLM_STATE["pause_skipped"] = set()
    elif saw_marker:
        _LLM_STATE["paused"] = paused
    if used == 0:
        _LLM_STATE["daily_recorded"] = False  # new UTC day → can warn again
        _LLM_STATE["daily_cost_recorded"] = False
    try:
        from .seams import resolve
        version, body = resolve(rt.graph, "setting.daily_cost_cap_usd", None)
        _LLM_STATE["cost_cap_override"] = (
            float(str(body).strip()) if version and body is not None else None)
    except Exception:
        pass
    return used


def lab_paused() -> bool:
    return bool(_LLM_STATE["paused"])


def set_lab_paused(graph, paused: bool, *, by: str = "operator") -> None:
    """Flip the global pause (ADR-015): append the marker event (the durable
    bit) and update in-process state. Restart-proof by construction —
    sync_daily_budget rebuilds from the markers at boot."""
    from .behaviors import emit_lab_event
    emit_lab_event(graph, "lab.paused" if paused else "lab.resumed",
                   {"by": by})
    _LLM_STATE["paused"] = paused
    _LLM_STATE["pause_skipped"] = set()  # new episode either way


def daily_cost_today() -> Decimal:
    return _LLM_STATE["daily_cost"]


def reset_llm_session() -> None:
    _LLM_STATE.update(total=0, by_behavior={}, anomalies=[],
                      budget_recorded=False, daily_used=0,
                      daily_recorded=False, daily_cost=Decimal("0"),
                      daily_cost_recorded=False, cost_cap_override=None,
                      paused=False, pause_skipped=set(), last_model=None)


def reset_llm_run_counters() -> None:
    """Reset the per-behavior-run counters. Call once per run cycle (the
    server and the overnight runner do this before each run_until_idle)."""
    _LLM_STATE["by_behavior"] = {}


def llm_usage() -> dict[str, Any]:
    return {"total": _LLM_STATE["total"],
            "by_behavior": dict(_LLM_STATE["by_behavior"]),
            "last_model": _LLM_STATE["last_model"]}


def consume_llm_anomalies(graph) -> list[str]:
    """Record queued LLM anomalies (budget exhaustion, parse failures) as
    observations. Called by every lab llm handler on entry; the session-wide
    budget observation is recorded at most once."""
    recorded = []
    while _LLM_STATE["anomalies"]:
        a = _LLM_STATE["anomalies"].pop(0)
        if a["kind"] == "paused":
            # ADR-015: one behavior-skipped observation per behavior per
            # pause episode (queue-side dedup) — never one per event.
            text = (f"Behavior skipped while paused: "
                    f"{a['behavior'] or 'an LLM behavior'} would have fired but "
                    "the lab is paused (operator control). It will fire again "
                    "on its next trigger after resume.")
            lab_kind = "behavior_skipped"
        elif a["kind"] == "budget":
            daily_cost = "daily cost cap" in a["detail"]
            daily = (not daily_cost) and "daily cap" in a["detail"]
            flag = ("daily_cost_recorded" if daily_cost
                    else "daily_recorded" if daily else "budget_recorded")
            if _LLM_STATE[flag]:
                continue
            _LLM_STATE[flag] = True
            text = (f"LLM budget exhausted: {a['detail']}. The lab stops making "
                    "model calls cleanly; "
                    + ("idle until the UTC day resets." if (daily or daily_cost)
                       else "queued work resumes next session."))
            lab_kind = "llm_budget"
        else:
            text = (f"LLM output parse failure in {a['behavior'] or 'unknown behavior'}: "
                    f"{a['detail']}. Salvaged what parsed; raw output attached.")
            lab_kind = "llm_parse_failure"
        obs = graph.add_object("observation", {
            "text": text,
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": lab_kind, "behavior": a["behavior"],
                         "raw_output": (a.get("raw") or "")[:2000]},
        })
        recorded.append(obs.id)
    return recorded


def _salvage_parse(raw_text: str, output_schema: type) -> Any:
    """Best-effort recovery of structured output from messy model text:
    strip code fences, find the outermost JSON object, validate."""
    if not raw_text or output_schema is None:
        return None
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            return output_schema.model_validate(json.loads(c))
        except Exception:
            continue
    return None


INERT_MARK = "[lab-inert]"


def is_inert(text: Optional[str]) -> bool:
    return bool(text) and text.startswith(INERT_MARK)


def _inert_output(output_schema: type, note: str) -> Any:
    """A schema-shaped do-nothing output so the runtime still invokes the
    handler (the runtime skips handlers entirely when parsed is None, which
    would swallow the anomaly). Marked with INERT_MARK; handlers no-op on it
    after recording the queued anomaly."""
    if output_schema is None:
        return None
    note = f"{INERT_MARK} {note}"
    name = getattr(output_schema, "__name__", "")
    try:
        if name == "PlanProposal":
            return output_schema(should_branch=False, reasoning=note)
        if name == "InterpretSummary":
            return output_schema(summary=note, outcome="decided")
        if name == "AnswerReply":
            return output_schema(reply=note)
        if name == "ResearchSynthesis":
            return output_schema(summary=note, findings=[])
        if name == "BlogDraft":
            return output_schema(title=note, slug="", body_markdown="")
    except Exception:
        pass
    try:
        return output_schema.model_construct()
    except Exception:
        return None


class LabProviderWrapper:
    """Budget enforcement + malformed-output salvage around any LLMProvider.

    - max_total: hard session cap; past it every call returns an inert,
      schema-valid output and queues a budget anomaly (recorded once as an
      observation by the next handler).
    - max_per_behavior: per-behavior-run cap; counters reset via
      reset_llm_run_counters(). The behavior is identified by matching the
      lab's prompt bodies against the assembled system text.
    - parse salvage: when the inner provider returns no parsed output, try
      to recover JSON from raw_text; failing that, return an inert output
      and queue a parse anomaly with the raw text attached.
    Never raises into the runtime.
    """

    def __init__(self, inner: Any, *, max_total: int = 60, max_per_behavior: int = 5,
                 max_daily: int = 200, max_daily_cost_usd: float = 50.0,
                 prompt_bodies: Optional[dict[str, str]] = None) -> None:
        self._inner = inner
        self._max_total = max_total
        self._max_per_behavior = max_per_behavior
        self._max_daily = max_daily
        self._max_daily_cost = max_daily_cost_usd
        self._prompts = prompt_bodies or {}
        self.default_model = getattr(inner, "default_model", "unknown")

    def effective_cost_cap(self) -> float:
        """ADR-015/019: the seam-approved override wins, else the settings
        value — and EVERY result clamps to the kernel's absolute ceiling.
        Tuning the cap is self-modification through the gate; moving the
        ceiling is a git change."""
        override = _LLM_STATE.get("cost_cap_override")
        cap = float(override) if override is not None else self._max_daily_cost
        return min(cap, ABSOLUTE_DAILY_COST_CEILING_USD)

    def _behavior_for(self, system: str) -> Optional[str]:
        for name, body in self._prompts.items():
            probe = body.strip()[:160]
            if probe and probe in (system or ""):
                return name
        return None

    def complete(self, **kwargs: Any) -> LLMResponse:
        schema = kwargs.get("output_schema")
        behavior = self._behavior_for(kwargs.get("system", ""))

        def _canned(note: str) -> LLMResponse:
            parsed = _inert_output(schema, note)
            return LLMResponse(
                raw_text=json.dumps({"lab": note}), parsed=parsed,
                input_tokens=0, output_tokens=0, cost_usd=Decimal("0"),
                latency_seconds=0.0,
                model=kwargs.get("model") or self.default_model,
                finish_reason="stop",
            )

        # ADR-015: while paused, every LLM behavior except answer idles. The
        # skip anomaly is queued once per behavior per pause episode — the
        # next lab handler records it as a behavior-skipped observation.
        if _LLM_STATE["paused"] and behavior != "answer":
            key = behavior or "?"
            if key not in _LLM_STATE["pause_skipped"]:
                _LLM_STATE["pause_skipped"].add(key)
                _LLM_STATE["anomalies"].append({
                    "kind": "paused", "behavior": behavior,
                    "detail": "lab is paused (operator control)", "raw": None})
            return _canned("lab paused")
        if _LLM_STATE["daily_used"] >= self._max_daily:
            _LLM_STATE["anomalies"].append({
                "kind": "budget", "behavior": behavior,
                "detail": f"daily cap {self._max_daily} reached (UTC reset)",
                "raw": None})
            return _canned("llm budget exhausted (daily cap)")
        cost_cap = self.effective_cost_cap()
        if float(_LLM_STATE["daily_cost"]) >= cost_cap:
            _LLM_STATE["anomalies"].append({
                "kind": "budget", "behavior": behavior,
                "detail": (f"daily cost cap ${cost_cap:.2f} reached "
                           f"(${float(_LLM_STATE['daily_cost']):.2f} spent today, "
                           "UTC reset)"),
                "raw": None})
            return _canned("llm budget exhausted (daily cost cap)")
        if _LLM_STATE["total"] >= self._max_total:
            _LLM_STATE["anomalies"].append({
                "kind": "budget", "behavior": behavior,
                "detail": f"session cap {self._max_total} reached", "raw": None})
            return _canned("llm budget exhausted (session cap)")
        used = _LLM_STATE["by_behavior"].get(behavior or "?", 0)
        if used >= self._max_per_behavior:
            _LLM_STATE["anomalies"].append({
                "kind": "budget", "behavior": behavior,
                "detail": f"per-run cap {self._max_per_behavior} reached for "
                          f"{behavior or 'unidentified behavior'}", "raw": None})
            return _canned("llm budget exhausted (per-behavior cap)")

        _LLM_STATE["total"] += 1
        _LLM_STATE["daily_used"] += 1
        _LLM_STATE["by_behavior"][behavior or "?"] = used + 1
        _LLM_STATE["last_model"] = kwargs.get("model") or self.default_model

        try:
            resp = self._inner.complete(**kwargs)
            try:  # ADR-015: native cost accounting, mirrored for the ceiling
                _LLM_STATE["daily_cost"] += Decimal(str(resp.cost_usd or 0))
            except Exception:
                pass
        except Exception as exc:
            # Native providers RAISE on schema-parse failures (the raw model
            # text rides on payload_extras) — salvage from there before
            # falling back to an inert output.
            raw = (getattr(exc, "payload_extras", None) or {}).get("raw_text")
            if raw and schema is not None:
                salvaged = _salvage_parse(raw, schema)
                if salvaged is not None:
                    return LLMResponse(
                        raw_text=raw, parsed=salvaged,
                        input_tokens=0, output_tokens=0, cost_usd=Decimal("0"),
                        latency_seconds=0.0,
                        model=kwargs.get("model") or self.default_model,
                        finish_reason="stop",
                    )
            _LLM_STATE["anomalies"].append({
                "kind": "parse", "behavior": behavior,
                "detail": f"provider call failed: {type(exc).__name__}: "
                          f"{str(exc).splitlines()[0][:200]}",
                "raw": raw})
            return _canned(f"provider error: {type(exc).__name__}")

        if schema is not None and resp.parsed is None:
            salvaged = _salvage_parse(resp.raw_text or "", schema)
            if salvaged is not None:
                resp.parsed = salvaged
            else:
                _LLM_STATE["anomalies"].append({
                    "kind": "parse", "behavior": behavior,
                    "detail": "model output did not match the schema",
                    "raw": resp.raw_text})
                return _canned("unparseable model output")
        return resp

    def estimate_cost(self, **kwargs: Any) -> Decimal:
        try:
            return self._inner.estimate_cost(**kwargs)
        except Exception:
            return Decimal("0")

    def count_tokens(self, **kwargs: Any) -> int:
        try:
            return self._inner.count_tokens(**kwargs)
        except Exception:
            return 0

    def recognizes_model(self, name: str) -> bool:
        try:
            return bool(self._inner.recognizes_model(name))
        except Exception:
            return True


# ---------------------------------------------------------------- selection

# The provider this process runs (set by select_lab_provider). Model routing
# (ADR-019) consults it so a seam-routed model the active provider does not
# recognize never reaches the wire.
_ACTIVE_PROVIDER: dict[str, Any] = {"provider": None}


def active_provider() -> Any:
    return _ACTIVE_PROVIDER["provider"]


def current_cost_cap(settings_default: float) -> float:
    """The daily cost cap in force for display paths: seam override else the
    settings value, clamped to the kernel ceiling (ADR-019)."""
    override = _LLM_STATE.get("cost_cap_override")
    cap = float(override) if override is not None else float(settings_default)
    return min(cap, ABSOLUTE_DAILY_COST_CEILING_USD)


_PROVIDER_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_AUTODETECT_ORDER = ["anthropic", "openai"]
_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-20250514"}


def _lab_prompt_bodies() -> dict[str, str]:
    from pathlib import Path
    from activegraph.packs import load_prompts_from_dir
    d = Path(__file__).parent / "prompts"
    try:
        # "charter" is not a behavior: its body is injected into SEVERAL
        # behaviors' contexts (ADR-018), so probing it would misidentify
        # every charter behavior as "charter" in _behavior_for.
        return {p.name: p.body for p in load_prompts_from_dir(d)
                if p.name != "charter"}
    except Exception:
        return {}


def select_lab_provider(
    *,
    provider_pref: Optional[str] = None,
    model: Optional[str] = None,
    settings: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """Return (provider_instance, info) resolved from args + environment.

    Precedence: explicit args > LAB_LLM_PROVIDER / LAB_LLM_MODEL env vars >
    LabSettings.model (Anthropic only) > provider defaults. With no key, a
    LabMockProvider keeps the full pipeline running deterministically.

    Live providers are wrapped in LabProviderWrapper: session/per-behavior
    call budgets and malformed-output salvage (A4). Keys come from env vars
    only and are read inside the native providers at call time — they never
    touch the graph, events, or logs.
    """
    pref = (provider_pref or os.environ.get("LAB_LLM_PROVIDER") or "").strip().lower()
    model = (model or os.environ.get("LAB_LLM_MODEL") or "").strip() or None

    chosen: Optional[str] = None
    if pref in _PROVIDER_KEY_ENV and os.environ.get(_PROVIDER_KEY_ENV[pref]):
        chosen = pref
    if chosen is None and pref in ("", "auto", "mock"):
        for p in _AUTODETECT_ORDER:
            if os.environ.get(_PROVIDER_KEY_ENV[p]):
                chosen = p
                break

    if chosen is None or pref == "mock":
        mock = LabMockProvider()
        _ACTIVE_PROVIDER["provider"] = mock
        return mock, {"mode": "mock", "provider": "mock", "model": None}

    if chosen == "anthropic" and model is None and settings is not None:
        model = getattr(settings, "model_default", None)

    inst: Any = OpenAIProvider() if chosen == "openai" else AnthropicProvider()
    inst.default_model = model or _DEFAULT_MODELS[chosen]
    wrapped = LabProviderWrapper(
        inst,
        max_total=getattr(settings, "max_total_llm_calls_per_session", 60),
        max_per_behavior=getattr(settings, "max_llm_calls_per_behavior_run", 5),
        max_daily=getattr(settings, "max_llm_calls_per_day", 200),
        max_daily_cost_usd=getattr(settings, "daily_cost_cap_usd", 50.0),
        prompt_bodies=_lab_prompt_bodies(),
    )
    _ACTIVE_PROVIDER["provider"] = wrapped
    return wrapped, {"mode": "live", "provider": chosen, "model": inst.default_model}
