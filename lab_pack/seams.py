"""Seam loader — KERNEL (ADR-012, Phase 4).

The lab's first self-modification surface: prompts, feed narration
templates, and whitelisted settings values live in the graph as artifacts
(kind=seam) and are promoted through decisions (kind=self_modify) that the
gate treats exactly like publish — absolute. Files remain the fallback, so
behavior is unchanged until a seam is approved.

Seam artifact convention:
    kind="seam", content=body,
    metadata={"lab": "seam", "seam_name", "version", "parent_version"}
    seam_name ∈ prompt.<behavior> | template.feed.<kind> | setting.<name>
    version: int, monotonic per seam_name

Resolution: highest-version APPROVED artifact for the name, else the
file/default. Hot-loads on approval (the gate calls hot_load) — no restart.
Kernel enforcement: bodies referencing the KERNEL manifest are refused at
proposal time AND re-checked at load time; only whitelisted settings are
seam-eligible (lab_pack/kernel.py, itself kernel).

Replay fidelity (4c): behaviors stamp the seam versions they consumed onto
the objects they create (version 0 = file default) — and since replay never
re-fires behaviors, those recorded outputs replay verbatim. Projection
templates resolve AS-OF the entry's event, via the approval events in the
log, so old entries keep rendering with the version that was active then.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .kernel import SEAM_ELIGIBLE_SETTINGS, kernel_reference

_PROMPT_BEHAVIORS = ("plan", "interpret", "answer", "draft_writer",
                     "research_worker", "seam_writer")

# ── the charter (ADR-018) ────────────────────────────────────────────────────
# An operator-authored mission charter, itself a seam: versioned, gated,
# hot-loaded, replay-recorded (behaviors stamp charter.mission alongside
# their prompt versions). v1 ships as the FILE DEFAULT — operator-authored
# content committed through the operator's own build pipeline — so unlike
# other seams the file default is version 1, not 0; future versions arrive
# only through the gate. The body is injected VERBATIM as a delimited
# CHARTER block into the context assembly (the behavior description) of
# plan, interpret, and draft_writer. answer is excluded: it speaks to the
# operator from graph state and must not narrate charter priorities as if
# they were its own findings.
CHARTER_SEAM = "charter.mission"
CHARTER_BEHAVIORS = ("plan", "interpret", "draft_writer")

_CHARTER_FILE = Path(__file__).parent / "prompts" / "charter.md"
_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.S)


def charter_file_default() -> str:
    """The operator's charter body, verbatim (the file's TOML frontmatter is
    loader metadata required by load_prompts_from_dir, not charter content)."""
    try:
        text = _CHARTER_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _FRONTMATTER_RE.sub("", text).strip("\n")


def charter_block(version: int, body: str) -> str:
    """The delimited CHARTER block appended to a charter behavior's context."""
    return (f"\n\n===== CHARTER (charter.mission v{version} — "
            "operator-authored, injected verbatim) =====\n"
            f"{body}\n===== END CHARTER =====")


def _cached_charter() -> tuple[int, str]:
    """Cache-only charter resolution (behaviors and hot_load run with a
    restricted graph): the approved override if one was ever loaded, else
    the file default at version 1."""
    hit = _CACHE.get(CHARTER_SEAM)
    if hit is not None:
        return hit
    return 1, charter_file_default()


def active_charter(graph) -> tuple[int, str]:
    """(version, body) of the charter in force: highest approved
    charter.mission seam, else the file default as version 1 (ADR-018)."""
    version, body = resolve(graph, CHARTER_SEAM, None)
    if version == 0 or body is None:
        return 1, charter_file_default()
    return version, body


def composed_description(behavior_name: str, prompt_body: str,
                         charter_version: int, charter_text: str) -> str:
    """A behavior's full context-assembly description: the prompt body, plus
    the verbatim CHARTER block for the charter behaviors. The prompt body
    stays the PREFIX so provider-side behavior identification (which probes
    prompt prefixes) is unaffected."""
    if behavior_name not in CHARTER_BEHAVIORS:
        return prompt_body
    return prompt_body + charter_block(charter_version, charter_text)


# ── model routing (ADR-019) ──────────────────────────────────────────────────
# Per-behavior model selection as seam settings (setting.model.<behavior>,
# setting.model.default). The resolution is stamped onto behavior.model —
# the attribute the runtime reads at prompt assembly and records natively
# on every llm.requested event, so the per-behavior resolution is in the
# log without any lab-side event plumbing. Cost accounting stays the
# runtime's native per-model reporting (cost_usd on llm.responded).

MODEL_ROUTED_BEHAVIORS = ("plan", "interpret", "draft_writer", "answer",
                          "research_worker", "code_worker", "code_author")

# The diff-authoring stage (ADR-037) shares the code_worker model tier — code
# authoring is top-tier reasoning, same plane as synthesis — so it resolves
# through setting.model.code_worker, not a separate routing entry.
_MODEL_KEY_ALIASES = {"code_author": "code_worker"}


def resolve_behavior_model(graph, behavior_name: str) -> str:
    """The model a lab llm_behavior should run: approved seam override,
    else the LabSettings default; behaviors without their own routing
    entry resolve through model.default."""
    from .settings import LabSettings
    defaults = LabSettings()
    key_name = _MODEL_KEY_ALIASES.get(behavior_name, behavior_name)
    key = (f"model.{key_name}" if key_name in MODEL_ROUTED_BEHAVIORS
           else "model.default")
    return str(effective_setting(graph, defaults, key)
               or defaults.model_default).strip()


def apply_model_routing(graph, provider: Any = None) -> dict[str, str]:
    """Stamp the per-behavior model resolution onto the live behaviors —
    at boot (after apply_approved) and on setting.model.* hot-loads, so a
    seam override takes effect without restart. A model the active provider
    does not recognize is skipped (the provider default stays), never an
    error: cross-provider misconfiguration must not kill the worker.
    Returns {behavior: model} actually applied."""
    from . import behaviors as lb
    applied: dict[str, str] = {}
    for name in MODEL_ROUTED_BEHAVIORS:
        model = resolve_behavior_model(graph, name)
        if not model:
            continue
        if provider is not None:
            try:
                if not provider.recognizes_model(model):
                    continue
            except Exception:
                pass
        # Module original and the runtime's registered copy alike — the
        # runtime reads ITS copy's .model at prompt assembly.
        for b in lb.behaviors_named(name):
            try:
                setattr(b, "model", model)
            except Exception:
                object.__setattr__(b, "model", model)
            applied[name] = model
    return applied

# resolve() cache: seam_name -> (version, body) | None (= file default).
# Invalidated by hot_load on approval and by clear_seam_cache().
_CACHE: dict[str, Optional[tuple[int, str]]] = {}


# Highest version SEEN per seam_name, pending proposals included — the
# version counter behaviors can use (BehaviorGraph cannot scan artifacts).
# Fed by propose_seam_fn/hot_load and rebuilt from the graph on resume.
_VERSIONS: dict[str, int] = {}


def note_seam_version(seam_name: str, version: int) -> None:
    _VERSIONS[seam_name] = max(int(version or 0), _VERSIONS.get(seam_name, 0))


def clear_seam_cache() -> None:
    _CACHE.clear()
    _VERSIONS.clear()


def _valid_name(seam_name: str) -> Optional[str]:
    """Return an error string if the seam name is not allowed, else None."""
    if seam_name.startswith("prompt."):
        if seam_name.split(".", 1)[1] not in _PROMPT_BEHAVIORS:
            return f"unknown prompt seam (known: {', '.join(_PROMPT_BEHAVIORS)})"
        return None
    if seam_name.startswith("charter."):
        # ADR-018: exactly one charter surface is whitelisted.
        if seam_name != CHARTER_SEAM:
            return f"unknown charter seam (known: {CHARTER_SEAM})"
        return None
    if seam_name.startswith("template.feed."):
        return None  # projection-only; kernel scan still applies to the body
    if seam_name.startswith("setting."):
        name = seam_name.split(".", 1)[1]
        if name not in SEAM_ELIGIBLE_SETTINGS:
            return (f"setting '{name}' is not seam-eligible "
                    f"(whitelist: {', '.join(sorted(SEAM_ELIGIBLE_SETTINGS))})")
        return None
    return "seam_name must be prompt.* | charter.mission | template.feed.* | setting.*"


def _seam_artifacts(graph, seam_name: str) -> list:
    if not hasattr(graph, "objects"):
        return []  # BehaviorGraph — the version registry stands in
    return [a for a in graph.objects(type="artifact")
            if a.data.get("kind") == "seam"
            and (a.data.get("metadata") or {}).get("seam_name") == seam_name]


def propose_seam_fn(graph, seam_name: str, body: str, rationale: str = "",
                    evidence_refs: Optional[list[str]] = None,
                    request_id: Optional[str] = None,
                    requested_by: str = "lab.seams"):
    """Propose a seam override: artifact (draft) + pending self_modify decision.

    `evidence_refs` (Phase 4): the ids that informed the proposal (rejected
    decisions, operator messages) — recorded on the decision's evidence_refs
    and the artifact's metadata, so the proposal EVENT carries them.

    Refusals (bad name, non-whitelisted setting, kernel reference) are
    graph-visible: a seam_refused observation is recorded and None returned —
    no artifact, no decision, nothing for anyone to approve.
    """
    err = _valid_name(seam_name)
    if err is None:
        hit = kernel_reference(body)
        if hit:
            err = f"body references kernel manifest entry '{hit}'"
    if err:
        graph.add_object("observation", {
            "text": f"Seam proposal refused for {seam_name}: {err}.",
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "seam_refused", "seam_name": seam_name},
        })
        return None

    existing = _seam_artifacts(graph, seam_name)
    versions = [int((a.data.get("metadata") or {}).get("version") or 0) for a in existing]
    # ADR-018: the charter's file default IS version 1 (operator-authored),
    # so the first graph-stored proposal is v2; every other seam starts at 1.
    # The registry covers behavior context, where artifacts cannot be scanned.
    base = 1 if seam_name == CHARTER_SEAM else 0
    version = max(versions + [_VERSIONS.get(seam_name, 0), base]) + 1
    note_seam_version(seam_name, version)
    active = active_version(graph, seam_name)

    meta = {"lab": "seam", "seam_name": seam_name,
            "version": version, "parent_version": active}
    if evidence_refs:
        meta["evidence_refs"] = list(evidence_refs)
    if request_id:
        meta["request_id"] = request_id
    artifact = graph.add_object("artifact", {
        "kind": "seam",
        "title": f"{seam_name} v{version}",
        "content": body,
        "format": "plain_text",
        "status": "draft",
        "metadata": meta,
    })
    graph.add_object("decision", {
        "subject_ref": artifact.id,
        "kind": "self_modify",
        "status": "pending",
        "rationale": rationale or f"Promote {seam_name} to v{version}.",
        "evidence_refs": [artifact.id] + list(evidence_refs or []),
        "metadata": {"requested_by": requested_by, "seam_name": seam_name,
                     "version": version},
    })
    return artifact


def resolve(graph, seam_name: str, default: Optional[str] = None) -> tuple[int, Optional[str]]:
    """(version, body) of the highest approved seam, or (0, default).

    Inside behaviors the graph is a restricted BehaviorGraph with no
    collection scans, so resolution is cache-only there — correct because
    hot_load (on every approval) and apply_approved (at boot/resume) are the
    only ways a seam becomes active, and both populate the cache."""
    if seam_name in _CACHE:
        hit = _CACHE[seam_name]
        return hit if hit is not None else (0, default)
    if not hasattr(graph, "objects"):
        return (0, default)  # BehaviorGraph + cache miss → file default
    best: Optional[tuple[int, str]] = None
    for a in _seam_artifacts(graph, seam_name):
        if a.data.get("status") != "approved":
            continue
        v = int((a.data.get("metadata") or {}).get("version") or 0)
        body = a.data.get("content") or ""
        # Defense in depth: an approved body that references the kernel is
        # never loaded, however it got approved.
        if kernel_reference(body):
            continue
        if best is None or v > best[0]:
            best = (v, body)
    _CACHE[seam_name] = best
    return best if best is not None else (0, default)


def active_version(graph, seam_name: str) -> int:
    if seam_name == CHARTER_SEAM:
        return active_charter(graph)[0]  # file default is v1 (ADR-018)
    return resolve(graph, seam_name, None)[0]


def effective_setting(graph, settings: Any, name: str) -> Any:
    """A whitelisted setting's value: approved seam override, else the
    pydantic settings value. Dotted seam names map to underscored fields
    (model.plan → model_plan, ADR-019). The override is coerced to the
    settings field's own type (str, int, or float); an uncoercible or empty
    body falls back to the default."""
    default = getattr(settings, name.replace(".", "_"))
    if name not in SEAM_ELIGIBLE_SETTINGS:
        return default
    version, body = resolve(graph, f"setting.{name}", None)
    if version == 0 or body is None:
        return default
    if isinstance(default, str):
        return str(body).strip() or default
    try:
        caster = float if isinstance(default, float) else int
        return caster(str(body).strip())
    except ValueError:
        return default


def seam_versions_stamp(graph, *names: str) -> dict[str, int]:
    """4c: the versions a behavior is about to consume (0 = file default),
    recorded onto the objects it creates."""
    return {n: active_version(graph, n) for n in names}


def hot_load(graph, artifact_id: str) -> None:
    """Apply an approved seam immediately — no restart. Called by the gate.

    prompt.* seams mutate the live behavior's description (the runtime
    assembles every prompt from it at call time); template/setting seams
    only need the cache dropped — they resolve per use.
    """
    artifact = graph.get_object(artifact_id)
    if artifact is None or artifact.data.get("kind") != "seam":
        return
    meta = artifact.data.get("metadata") or {}
    seam_name = meta.get("seam_name") or ""
    body = artifact.data.get("content") or ""
    version = int(meta.get("version") or 0)

    if kernel_reference(body) or _valid_name(seam_name):
        graph.add_object("observation", {
            "text": (f"Seam {seam_name} v{meta.get('version')} approved but "
                     "REFUSED at load time (kernel reference or invalid name). "
                     "The file default stays active."),
            "confidence": 1.0,
            "category": "risk",
            "metadata": {"lab": "seam_refused", "seam_name": seam_name},
        })
        return

    # Populate the cache directly: gate calls hot_load with a restricted
    # BehaviorGraph, and behaviors resolve cache-only — this write IS the
    # hot-load. Never regress to a lower version.
    current = _CACHE.get(seam_name)
    if not current or version >= current[0]:
        _CACHE[seam_name] = (version, body)
    note_seam_version(seam_name, version)

    if seam_name.startswith("prompt."):
        _apply_prompt(seam_name.split(".", 1)[1], body)
    elif seam_name.startswith("setting.model."):
        # ADR-019: model rerouting takes effect without restart.
        from .llm import active_provider
        apply_model_routing(graph, active_provider())
    elif seam_name == CHARTER_SEAM:
        # ADR-018: a new charter recomposes every charter behavior's context
        # with its active prompt body (cache-only, like everything in here).
        from . import behaviors as lb
        for name in CHARTER_BEHAVIORS:
            hit = _CACHE.get(f"prompt.{name}")
            _apply_prompt(name, hit[1] if hit else lb._PROMPTS.get(name, ""))


def _apply_prompt(behavior_name: str, body: str) -> None:
    from . import behaviors as lb  # late import — seams is kernel, no cycle
    version, charter = _cached_charter()
    text = composed_description(behavior_name, body, version, charter)
    # Both the module original AND the runtime's registered copy: the pack
    # loader registers fresh canonical-named behaviors, so mutating only the
    # original would leave the live runtime on the old prompt.
    for b in lb.behaviors_named(behavior_name):
        try:
            setattr(b, "description", text)
        except Exception:
            object.__setattr__(b, "description", text)


def apply_approved(graph) -> int:
    """Boot/resume: re-apply every active approved seam (replay rebuilds the
    artifacts but not the in-process behavior descriptions). Returns count."""
    clear_seam_cache()
    applied = 0
    names = {(a.data.get("metadata") or {}).get("seam_name")
             for a in graph.objects(type="artifact")
             if a.data.get("kind") == "seam" and a.data.get("status") == "approved"}
    # charter.mission sorts before prompt.*, so an approved charter is cached
    # before any prompt recomposition reads it (ADR-018).
    for name in sorted(n for n in names if n):
        version, body = resolve(graph, name, None)
        if not version or body is None:
            continue
        if name == CHARTER_SEAM:
            from . import behaviors as lb
            for b_name in CHARTER_BEHAVIORS:
                hit = _CACHE.get(f"prompt.{b_name}")
                _apply_prompt(b_name, hit[1] if hit else lb._PROMPTS.get(b_name, ""))
            applied += 1
        elif name.startswith("prompt."):
            _apply_prompt(name.split(".", 1)[1], body)
            applied += 1
    # ADR-019: model routing re-stamps at boot whether or not a model seam
    # exists — the LabSettings defaults are the routing table's floor.
    from .llm import active_provider
    apply_model_routing(graph, active_provider())
    return applied


def seam_status(graph) -> dict:
    """For the Seams view: every known seam surface, its active version and
    source (file|graph), plus pending proposals."""
    from .settings import LabSettings
    surfaces: list[dict] = []
    known = [CHARTER_SEAM]
    known += [f"prompt.{b}" for b in _PROMPT_BEHAVIORS]
    known += [f"setting.{s}" for s in sorted(SEAM_ELIGIBLE_SETTINGS)]
    template_names = {(a.data.get("metadata") or {}).get("seam_name")
                      for a in graph.objects(type="artifact")
                      if a.data.get("kind") == "seam"
                      and str((a.data.get("metadata") or {}).get("seam_name", ""))
                      .startswith("template.feed.")}
    known += sorted(n for n in template_names if n)

    pending = {}
    for d in graph.objects(type="decision"):
        if d.data.get("kind") == "self_modify" and d.data.get("status") == "pending":
            n = (d.data.get("metadata") or {}).get("seam_name")
            if n:
                pending.setdefault(n, []).append(d.id)

    defaults = LabSettings()
    for name in known:
        version, _ = resolve(graph, name, None)
        # ADR-018: the charter's file default is itself v1 (operator-authored).
        if name == CHARTER_SEAM and version == 0:
            entry = {"seam_name": name, "active_version": 1, "source": "file",
                     "pending": pending.get(name, [])}
            surfaces.append(entry)
            continue
        entry = {
            "seam_name": name,
            "active_version": version,
            "source": "graph" if version else "file",
            "pending": pending.get(name, []),
        }
        if name.startswith("setting."):
            entry["effective_value"] = effective_setting(
                graph, defaults, name.split(".", 1)[1])
        surfaces.append(entry)
    # Templates can be proposed for kinds with no prior seam — surface those too.
    for name, ids in pending.items():
        if not any(s["seam_name"] == name for s in surfaces):
            surfaces.append({"seam_name": name, "active_version": 0,
                             "source": "file", "pending": ids})
    return {"seams": surfaces,
            "whitelisted_settings": sorted(SEAM_ELIGIBLE_SETTINGS)}
