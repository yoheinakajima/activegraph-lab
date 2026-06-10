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


class BlogDraft(BaseModel):
    """Structured output for the draft_writer behavior."""

    title: str = Field(description="Post title, plain and specific. No hype.")
    slug: str = Field(
        default="",
        description="URL slug (lowercase, hyphens). Derived from the title if empty.",
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
            parsed = BlogDraft(
                title=f"Lab note: {finding[:60].rstrip('. ')}",
                slug=f"lab-note-{digest}",
                body_markdown=(
                    f"## What I tried\n\nI followed the mission's loop on this finding: "
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
    "last_model": None,
}


def reset_llm_session() -> None:
    _LLM_STATE.update(total=0, by_behavior={}, anomalies=[],
                      budget_recorded=False, last_model=None)


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
        if a["kind"] == "budget":
            if _LLM_STATE["budget_recorded"]:
                continue
            _LLM_STATE["budget_recorded"] = True
            text = (f"LLM budget exhausted: {a['detail']}. The lab stops making "
                    "model calls cleanly; queued work resumes next session.")
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
                 prompt_bodies: Optional[dict[str, str]] = None) -> None:
        self._inner = inner
        self._max_total = max_total
        self._max_per_behavior = max_per_behavior
        self._prompts = prompt_bodies or {}
        self.default_model = getattr(inner, "default_model", "unknown")

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
        _LLM_STATE["by_behavior"][behavior or "?"] = used + 1
        _LLM_STATE["last_model"] = kwargs.get("model") or self.default_model

        try:
            resp = self._inner.complete(**kwargs)
        except Exception as exc:
            _LLM_STATE["anomalies"].append({
                "kind": "parse", "behavior": behavior,
                "detail": f"provider call failed: {type(exc).__name__}: {exc}",
                "raw": None})
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

_PROVIDER_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_AUTODETECT_ORDER = ["anthropic", "openai"]
_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-20250514"}


def _lab_prompt_bodies() -> dict[str, str]:
    from pathlib import Path
    from activegraph.packs import load_prompts_from_dir
    d = Path(__file__).parent / "prompts"
    try:
        return {p.name: p.body for p in load_prompts_from_dir(d)}
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
        return LabMockProvider(), {"mode": "mock", "provider": "mock", "model": None}

    if chosen == "anthropic" and model is None and settings is not None:
        model = getattr(settings, "model", None)

    inst: Any = OpenAIProvider() if chosen == "openai" else AnthropicProvider()
    inst.default_model = model or _DEFAULT_MODELS[chosen]
    wrapped = LabProviderWrapper(
        inst,
        max_total=getattr(settings, "max_total_llm_calls_per_session", 60),
        max_per_behavior=getattr(settings, "max_llm_calls_per_behavior_run", 5),
        prompt_bodies=_lab_prompt_bodies(),
    )
    return wrapped, {"mode": "live", "provider": chosen, "model": inst.default_model}
