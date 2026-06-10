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
from typing import Any, Optional

from .kernel import SEAM_ELIGIBLE_SETTINGS, kernel_reference

_PROMPT_BEHAVIORS = ("plan", "interpret", "answer", "draft_writer")

# resolve() cache: seam_name -> (version, body) | None (= file default).
# Invalidated by hot_load on approval and by clear_seam_cache().
_CACHE: dict[str, Optional[tuple[int, str]]] = {}


def clear_seam_cache() -> None:
    _CACHE.clear()


def _valid_name(seam_name: str) -> Optional[str]:
    """Return an error string if the seam name is not allowed, else None."""
    if seam_name.startswith("prompt."):
        if seam_name.split(".", 1)[1] not in _PROMPT_BEHAVIORS:
            return f"unknown prompt seam (known: {', '.join(_PROMPT_BEHAVIORS)})"
        return None
    if seam_name.startswith("template.feed."):
        return None  # projection-only; kernel scan still applies to the body
    if seam_name.startswith("setting."):
        name = seam_name.split(".", 1)[1]
        if name not in SEAM_ELIGIBLE_SETTINGS:
            return (f"setting '{name}' is not seam-eligible "
                    f"(whitelist: {', '.join(sorted(SEAM_ELIGIBLE_SETTINGS))})")
        return None
    return "seam_name must be prompt.* | template.feed.* | setting.*"


def _seam_artifacts(graph, seam_name: str) -> list:
    return [a for a in graph.objects(type="artifact")
            if a.data.get("kind") == "seam"
            and (a.data.get("metadata") or {}).get("seam_name") == seam_name]


def propose_seam_fn(graph, seam_name: str, body: str, rationale: str = ""):
    """Propose a seam override: artifact (draft) + pending self_modify decision.

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
    version = (max(versions) + 1) if versions else 1
    active = active_version(graph, seam_name)

    artifact = graph.add_object("artifact", {
        "kind": "seam",
        "title": f"{seam_name} v{version}",
        "content": body,
        "format": "plain_text",
        "status": "draft",
        "metadata": {"lab": "seam", "seam_name": seam_name,
                     "version": version, "parent_version": active},
    })
    graph.add_object("decision", {
        "subject_ref": artifact.id,
        "kind": "self_modify",
        "status": "pending",
        "rationale": rationale or f"Promote {seam_name} to v{version}.",
        "evidence_refs": [artifact.id],
        "metadata": {"requested_by": "lab.seams", "seam_name": seam_name,
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
    return resolve(graph, seam_name, None)[0]


def effective_setting(graph, settings: Any, name: str) -> Any:
    """A whitelisted setting's value: approved seam override, else the
    pydantic settings value. Type follows the settings field (int here)."""
    if name not in SEAM_ELIGIBLE_SETTINGS:
        return getattr(settings, name)
    version, body = resolve(graph, f"setting.{name}", None)
    if version == 0 or body is None:
        return getattr(settings, name)
    try:
        return int(str(body).strip())
    except ValueError:
        return getattr(settings, name)


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

    if seam_name.startswith("prompt."):
        _apply_prompt(seam_name.split(".", 1)[1], body)


def _apply_prompt(behavior_name: str, body: str) -> None:
    from . import behaviors as lb  # late import — seams is kernel, no cycle
    for b in lb.BEHAVIORS:
        if getattr(b, "name", None) == behavior_name:
            try:
                setattr(b, "description", body)
            except Exception:
                object.__setattr__(b, "description", body)
            return


def apply_approved(graph) -> int:
    """Boot/resume: re-apply every active approved seam (replay rebuilds the
    artifacts but not the in-process behavior descriptions). Returns count."""
    clear_seam_cache()
    applied = 0
    names = {(a.data.get("metadata") or {}).get("seam_name")
             for a in graph.objects(type="artifact")
             if a.data.get("kind") == "seam" and a.data.get("status") == "approved"}
    for name in sorted(n for n in names if n):
        version, body = resolve(graph, name, None)
        if version and name.startswith("prompt.") and body is not None:
            _apply_prompt(name.split(".", 1)[1], body)
            applied += 1
    return applied


def seam_status(graph) -> dict:
    """For the Seams view: every known seam surface, its active version and
    source (file|graph), plus pending proposals."""
    from .settings import LabSettings
    surfaces: list[dict] = []
    known = [f"prompt.{b}" for b in _PROMPT_BEHAVIORS]
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
