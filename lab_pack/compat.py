"""Relation call-convention compatibility (ADR-008).

The packs repo is split on add_relation argument order: core, research and
tool_gateway pass the relation TYPE first (so it lands in the `source` field),
while chat — and this lab — follow the real ``Graph.add_relation(source,
target, type)`` signature. A composed graph therefore holds both encodings.

This module is the ONE place the lab decodes relations. The discriminator:
object ids always contain ``#`` (``"type#n"``); relation type names never do.

This shim exists because of recorded upstream friction — see the
``upstream_friction`` observation and the "unify add_relation call convention"
issue-draft artifact seeded under the mission (lab_pack/bundle.py), both of
which reference ADR-008. If upstream standardizes on the signature order,
delete this module and read ``(r.type, r.source, r.target)`` directly.
"""

from __future__ import annotations


def decode_relation(r) -> tuple[str, str, str]:
    """Return ``(relation_type, source_id, target_id)`` for either encoding."""
    if "#" in str(r.type):
        # Type-first call: type landed in `source`, subject in `target`,
        # object in `type` (core/research/tool_gateway style).
        return str(r.source), str(r.target), str(r.type)
    return str(r.type), str(r.source), str(r.target)


def relation_touches(r, object_id: str) -> bool:
    """True if `object_id` is either endpoint of the relation, regardless of
    which encoding wrote it."""
    _, src, tgt = decode_relation(r)
    return object_id in (src, tgt)
