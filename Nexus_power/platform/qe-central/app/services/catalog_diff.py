"""P6 — catalog regression: diff two Master Catalog snapshots.

A re-crawl mints a new ``artifact_id`` and a new ``catalog_versions`` snapshot.
Diffing two snapshots by the stable ``question_id`` (P2, Δ2) turns the engine
into a change-detection system for a regulated application: it names exactly what
changed between crawls — a question added or removed, a branch moved (its expected
next page changed), a rule or validation changed — instead of only detecting
outcome drift on a single journey.

Pure + deterministic: two snapshots in, a structured diff out. No DB, no I/O.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Field → the change kind a reviewer sees. Value-free labels.
_FIELD_KIND = {
    "name": "renamed",
    "type": "type_changed",
    "required": "required_changed",
    "options": "options_changed",
    "validation": "validation_changed",
    "business_rule": "rule_changed",
    "expected_next_page": "moved_next_page",
}


def _by_id(snapshot: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for q in (snapshot.get("questions") or []):
        if isinstance(q, Mapping):
            qid = str(q.get("question_id") or "")
            if qid:
                out[qid] = q
    return out


def _answer_type(q: Mapping[str, Any]) -> Any:
    return q.get("answer_type") or q.get("type") or "text"


def _norm_options(v: Any) -> list[str]:
    if not isinstance(v, Sequence) or isinstance(v, (str, bytes)):
        return []
    return sorted(str(x) for x in v)


def diff_catalogs(
    old: Mapping[str, Any], new: Mapping[str, Any]
) -> dict[str, Any]:
    """Diff two Master Catalog snapshots by stable ``question_id``.

    Each snapshot is ``{"questions": [ {question_id, name, type/answer_type,
    required, options, validation, business_rule, expected_next_page}, ... ]}``
    (as produced by ``catalog.snapshot_catalog`` / ``build_master_catalog``).

    Returns ``{added, removed, changed, unchanged, summary}`` where ``changed`` is
    a list of ``{question_id, kinds, changes:{field:{from,to}}}``.
    """
    o, n = _by_id(old), _by_id(new)
    added = sorted(set(n) - set(o))
    removed = sorted(set(o) - set(n))

    changed: list[dict[str, Any]] = []
    for qid in sorted(set(o) & set(n)):
        oc, nc = o[qid], n[qid]
        changes: dict[str, Any] = {}
        kinds: list[str] = []
        for field in _FIELD_KIND:
            if field == "type":
                ov, nv = _answer_type(oc), _answer_type(nc)
            elif field == "options":
                ov, nv = _norm_options(oc.get("options")), _norm_options(nc.get("options"))
            else:
                ov, nv = oc.get(field), nc.get(field)
            if ov != nv:
                changes[field] = {"from": ov, "to": nv}
                kinds.append(_FIELD_KIND[field])
        if changes:
            changed.append({"question_id": qid, "kinds": kinds, "changes": changes})

    unchanged = len(set(o) & set(n)) - len(changed)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": unchanged,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
            "has_changes": bool(added or removed or changed),
        },
    }
