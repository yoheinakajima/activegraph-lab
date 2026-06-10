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


# ---------------------------------------------------------------- mock provider


def _extract_field(messages: list, field: str) -> str:
    """Pull a JSON string field (e.g. the triggering observation's text) out of
    the serialized prompt. Best-effort; used only to make mock output distinct."""
    blob = " ".join(str(getattr(m, "content", m)) for m in messages)
    found = re.findall(r'"%s":\s*"([^"]{10,200}?)"' % field, blob)
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


# ---------------------------------------------------------------- selection

_PROVIDER_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_AUTODETECT_ORDER = ["anthropic", "openai"]
_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-sonnet-4-5"}


def select_lab_provider(
    *, provider_pref: Optional[str] = None, model: Optional[str] = None
) -> tuple[Any, dict[str, Any]]:
    """Return (provider_instance, info) resolved from args + environment.

    Precedence: explicit args > LAB_LLM_PROVIDER / LAB_LLM_MODEL env vars >
    auto-detection from whichever provider key is present. With no key, a
    LabMockProvider keeps the full pipeline running deterministically.
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

    inst: Any = OpenAIProvider() if chosen == "openai" else AnthropicProvider()
    inst.default_model = model or _DEFAULT_MODELS[chosen]
    return inst, {"mode": "live", "provider": chosen, "model": inst.default_model}
