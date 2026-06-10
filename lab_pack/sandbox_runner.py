"""Graph-code sandbox runner — KERNEL (ADR-012, Phase 5b).

Executed as a SUBPROCESS (`python -I`) with a wall-clock timeout owned by the
parent. The draft source runs against a fake graph and stub decorators —
no runtime, no store, no network, no real packs — and the runner prints a
JSON report of everything the draft touched so the parent can enforce the
declared-scope check.

Usage (parent: lab_pack/graph_code.py):
    python -I lab_pack/sandbox_runner.py <draft_source_file> <subscriptions_json>
"""

from __future__ import annotations

import json
import sys
import types


class FakeObject:
    def __init__(self, id_, type_, data):
        self.id, self.type, self.data = id_, type_, data


class FakeGraph:
    """Records every mutation the draft attempts; that record IS the scope."""

    def __init__(self):
        self.touched_types: list[str] = []
        self.created = 0
        self._objects: dict[str, FakeObject] = {}

    def add_object(self, type_, data, **kw):
        self.touched_types.append(str(type_))
        self.created += 1
        obj = FakeObject(f"{type_}#{self.created}", type_, dict(data or {}))
        self._objects[obj.id] = obj
        return obj

    def patch_object(self, object_id, value, **kw):
        base = str(object_id).split("#")[0]
        self.touched_types.append(base)
        return None

    def add_relation(self, source, target, type_=None, **kw):
        self.touched_types.append(f"relation:{type_}")
        return None

    def get_object(self, object_id):
        return self._objects.get(object_id)

    def get_relation(self, *a, **kw):
        return None

    def emit(self, *a, **kw):
        raise RuntimeError("graph code may not emit raw events")


class FakeEvent:
    def __init__(self, obj_type):
        self.id = "evt_sandbox"
        self.type = "object.created"
        self.payload = {
            "object": {
                "id": f"{obj_type}#1",
                "type": obj_type,
                "data": {"text": "sandbox sample", "title": "sandbox sample",
                         "status": "active", "metadata": {"lab": "sandbox"}},
            },
            "id": f"{obj_type}#1",
        }
        self.actor = "sandbox"
        self.frame_id = None
        self.timestamp = None


def main() -> int:
    source_path, subs_json = sys.argv[1], sys.argv[2]
    subscriptions = json.loads(subs_json)
    with open(source_path) as f:
        source = f.read()

    captured = []

    def _decorator_factory(**_decl):
        def _wrap(fn):
            captured.append(fn)
            return fn
        return _wrap

    # Stub module tree so `from activegraph.packs import behavior` resolves to
    # capture-only decorators — the real runtime never loads in the sandbox.
    pkg = types.ModuleType("activegraph")
    packs = types.ModuleType("activegraph.packs")
    packs.behavior = _decorator_factory
    packs.llm_behavior = _decorator_factory
    packs.tool = _decorator_factory
    pkg.packs = packs
    sys.modules["activegraph"] = pkg
    sys.modules["activegraph.packs"] = packs

    namespace: dict = {"__name__": "graph_code_draft"}
    exec(compile(source, "<draft>", "exec"), namespace)  # noqa: S102 — sandboxed

    handlers = captured or [v for v in namespace.values() if callable(v)
                            and getattr(v, "__module__", "") == "graph_code_draft"]
    graph = FakeGraph()
    fired = 0
    for sub in subscriptions:
        event = FakeEvent(sub)
        for handler in handlers:
            try:
                handler(event, graph, None)
            except TypeError:
                handler(event, graph, None, settings=None)
            fired += 1

    print(json.dumps({
        "ok": True,
        "fired": fired,
        "created": graph.created,
        "touched": sorted(set(t for t in graph.touched_types
                              if not t.startswith("relation:"))),
        "relations": sorted(set(t.split(":", 1)[1] for t in graph.touched_types
                                if t.startswith("relation:"))),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
