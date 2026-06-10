"""Graph code machinery — KERNEL (ADR-012, Phase 5). Shipped DARK.

Behaviors and tools drafted as artifacts (kind=behavior_draft | tool_draft),
pushed through a pipeline where every step is an event:

    static checks (AST + kernel manifest)
      → sandbox run (subprocess, wall-clock timeout, fake graph)
      → declared-scope check (it touched only what it declared)
      → decision kind=self_modify, status pending

The runtime loader activates APPROVED drafts ONLY when LAB_ALLOW_GRAPH_CODE=1
— an approved decision alone is not enough (CONTRACT). Flag unset (the
default, and the deploy default): approved drafts are listed as dormant.
Loaded graph behaviors run with a tagging graph proxy: every object they
create carries metadata.graph_code = {artifact, version} provenance.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from .kernel import KERNEL_MANIFEST, kernel_reference

SANDBOX_TIMEOUT_SECONDS = 10
_RUNNER = Path(__file__).parent / "sandbox_runner.py"

# Modules a draft may never import: the kernel manifest plus process/network
# escape hatches. Network leaves the process only through tool_gateway.
_BANNED_IMPORT_PREFIXES = tuple(
    e for e in KERNEL_MANIFEST if "." in e or e in ("subprocess", "importlib")
) + (
    "os", "sys", "socket", "urllib", "requests", "http", "httpx", "aiohttp",
    "ftplib", "smtplib", "ctypes", "multiprocessing", "threading", "shutil",
    "pathlib", "builtins",
)
_BANNED_CALLS = {"exec", "eval", "compile", "__import__", "open", "getattr"}

# Loaded drafts: artifact_id -> behavior names (for the Seams view).
_LOADED: dict[str, list[str]] = {}


def graph_code_enabled() -> bool:
    return os.environ.get("LAB_ALLOW_GRAPH_CODE", "") == "1"


# ---------------------------------------------------------------- pipeline


def static_checks(source: str) -> list[str]:
    """AST + string-level scan. Returns violations (empty = pass)."""
    violations: list[str] = []
    hit = kernel_reference(source)
    if hit:
        violations.append(f"references kernel manifest entry '{hit}'")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return violations + [f"syntax error: {exc.msg} (line {exc.lineno})"]

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) \
                else [node.module or ""]
            for name in names:
                if any(name == p or name.startswith(p + ".")
                       for p in _BANNED_IMPORT_PREFIXES):
                    violations.append(f"banned import '{name}'")
                elif name.split(".")[0] not in ("activegraph", "json", "re",
                                                "math", "datetime", "typing",
                                                "collections", "itertools",
                                                "functools", "textwrap"):
                    violations.append(f"import outside the allowlist: '{name}'")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else None
            if name in _BANNED_CALLS:
                violations.append(f"banned call '{name}()'")
        elif isinstance(node, ast.Attribute):
            # os.environ reads (and anything os.*) — string scan catches
            # "os.environ" verbatim; this catches aliased Attribute chains.
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                violations.append(f"os.{node.attr} access")
    return sorted(set(violations))


def sandbox_run(source: str, subscriptions: list[str],
                timeout_seconds: float = SANDBOX_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run the draft in an isolated subprocess against a fake graph.
    Returns {"ok": bool, "touched": [...], "error": str|None, "timed_out": bool}."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(_RUNNER), path, json.dumps(subscriptions)],
            capture_output=True, text=True, timeout=timeout_seconds,
            env={"PATH": os.environ.get("PATH", "")},  # no secrets in the sandbox
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "touched": [], "timed_out": True,
                "error": f"wall-clock timeout after {timeout_seconds}s — killed"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if proc.returncode != 0:
        return {"ok": False, "touched": [], "timed_out": False,
                "error": (proc.stderr or "sandbox crashed").strip().splitlines()[-1][:200]}
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "touched": [], "timed_out": False,
                "error": "sandbox produced no report"}
    report.setdefault("timed_out", False)
    report.setdefault("error", None)
    return report


def propose_behavior_draft_fn(
    graph,
    name: str,
    source: str,
    subscriptions: list[str],
    touches: list[str],
    authority: str = "gated",
    kind: str = "behavior_draft",
    rationale: str = "",
    timeout_seconds: float = SANDBOX_TIMEOUT_SECONDS,
):
    """Run the full pipeline. Every step lands as an evaluation event; only a
    fully passing draft gets a pending self_modify decision. Returns the
    artifact (status draft if pipeline passed, rejected otherwise)."""
    artifact = graph.add_object("artifact", {
        "kind": kind,
        "title": f"{kind}: {name}",
        "content": source,
        "format": "plain_text",
        "status": "draft",
        "metadata": {"lab": "graph_code", "name": name,
                     "subscriptions": list(subscriptions),
                     "touches": list(touches), "authority": authority},
    })

    def _step(step: str, passed: bool, detail: str) -> bool:
        graph.add_object("evaluation", {
            "subject_id": artifact.id,
            "subject_type": "artifact",
            "judgment": "passed" if passed else "failed",
            "rationale": f"{step}: {detail}"[:500],
            "evaluator": "lab.graph_code",
            "metadata": {"lab": "graph_code_check", "step": step,
                         "artifact_id": artifact.id},
        })
        if not passed:
            graph.patch_object(artifact.id, {"status": "rejected"})
        return passed

    violations = static_checks(source)
    if not _step("static_checks", not violations,
                 "; ".join(violations) or "no banned imports, calls, or kernel references"):
        return artifact

    report = sandbox_run(source, subscriptions, timeout_seconds)
    if not _step("sandbox_run", bool(report.get("ok")),
                 report.get("error") or
                 f"fired {report.get('fired', 0)}x, touched {report.get('touched')}"):
        return artifact

    undeclared = [t for t in report.get("touched", []) if t not in touches]
    if not _step("scope_check", not undeclared,
                 f"undeclared types touched: {undeclared}" if undeclared
                 else f"touched ⊆ declared ({touches})"):
        return artifact

    graph.add_object("decision", {
        "subject_ref": artifact.id,
        "kind": "self_modify",
        "status": "pending",
        "rationale": rationale or (
            f"Promote {kind} '{name}' (subscribes: {', '.join(subscriptions)}; "
            f"touches: {', '.join(touches)}; authority: {authority}). "
            "Even if approved, it stays dormant until LAB_ALLOW_GRAPH_CODE=1."),
        "evidence_refs": [artifact.id],
        "metadata": {"requested_by": "lab.graph_code", "draft_name": name},
    })
    return artifact


# ---------------------------------------------------------------- loader


class _TaggingGraph:
    """Pass-through graph proxy that stamps graph-code provenance onto every
    object a loaded draft creates (5c: tagged in every event they emit —
    the object payload rides in the event)."""

    def __init__(self, inner, tag: dict):
        self._inner = inner
        self._tag = tag

    def add_object(self, type_, data, **kw):
        data = dict(data or {})
        meta = dict(data.get("metadata") or {})
        meta["graph_code"] = self._tag
        data["metadata"] = meta
        return self._inner.add_object(type_, data, **kw)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def load_approved_drafts(rt) -> int:
    """Load APPROVED drafts as live behaviors — ONLY under LAB_ALLOW_GRAPH_CODE=1.

    Static checks re-run at load time (defense in depth: approval does not
    bypass the manifest). Returns the number of drafts loaded; 0 when the
    flag is unset, however many approvals exist.
    """
    from activegraph.behaviors.base import Behavior
    from activegraph.packs import Pack

    if not graph_code_enabled():
        return 0
    g = rt.graph
    loaded = 0
    for a in g.objects(type="artifact"):
        if a.data.get("kind") not in ("behavior_draft", "tool_draft"):
            continue
        if a.data.get("status") != "approved" or a.id in _LOADED:
            continue
        meta = a.data.get("metadata") or {}
        source = a.data.get("content") or ""
        if static_checks(source):
            continue  # never loads, however it got approved
        tag = {"artifact": a.id, "name": meta.get("name"),
               "version": int(meta.get("version") or 1)}

        # The draft imports the REAL @behavior decorator (static checks only
        # allowlist activegraph.packs); exec yields real Behavior objects
        # whose handlers we wrap for provenance tagging.
        namespace: dict = {"__name__": f"graph_code_{a.id.replace('#', '_')}"}
        try:
            exec(compile(source, f"<graph_code {a.id}>", "exec"), namespace)  # noqa: S102
        except Exception:
            continue

        behaviors = []
        suffix = a.id.split("#")[-1]
        for value in list(namespace.values()):
            if not isinstance(value, Behavior):
                continue
            orig_handler = value.fn

            def _tagged(event, graph, ctx, _fn=orig_handler, _tag=tag, **kwargs):
                return _fn(event, _TaggingGraph(graph, _tag), ctx, **kwargs)

            for attr, val in (("fn", _tagged),
                              ("name", f"graph_code_{value.name}_{suffix}")):
                try:
                    setattr(value, attr, val)
                except Exception:
                    object.__setattr__(value, attr, val)
            behaviors.append(value)
        if not behaviors:
            continue
        pack = Pack(
            name=f"graph_code_{suffix}",
            version="0.0.1",
            description=f"Graph-code draft {meta.get('name')} ({a.id}), gated + flagged.",
            behaviors=tuple(behaviors),
        )
        if rt.load_pack(pack):
            _LOADED[a.id] = [b.name for b in behaviors]
            loaded += 1
    return loaded


def clear_loaded() -> None:
    _LOADED.clear()


def status(graph) -> dict:
    """For the Seams view: drafts with pipeline state and dormant/loaded."""
    drafts = []
    for a in graph.objects(type="artifact"):
        if a.data.get("kind") not in ("behavior_draft", "tool_draft"):
            continue
        meta = a.data.get("metadata") or {}
        st = a.data.get("status")
        if a.id in _LOADED:
            state = "loaded"
        elif st == "approved":
            state = "dormant (LAB_ALLOW_GRAPH_CODE unset)" \
                if not graph_code_enabled() else "dormant (not yet loaded)"
        elif st == "rejected":
            state = "rejected by pipeline or operator"
        else:
            state = "pipeline/pending"
        drafts.append({"id": a.id, "name": meta.get("name"),
                       "kind": a.data.get("kind"), "status": st, "state": state,
                       "subscriptions": meta.get("subscriptions"),
                       "touches": meta.get("touches")})
    return {"graph_code": drafts, "graph_code_enabled": graph_code_enabled()}
