"""E3 — Catalog service (surfacing the control inventory with provenance).

The crawl manifest already captures rich per-page-state data: form controls
with types/options/required, displayed outcome values with selectors, and a
field ledger with semantic types and signatures.  The journey graph stores
only coarse booleans per node (is_decision, is_boundary, has_outcome).

This service bridges the gap: it extracts the per-node control inventory
from the manifest's page states and field ledger during fold, and composes
catalog views at query time with honest provenance badges.

Provenance is computed at QUERY TIME, not stored:

  * **observed** — the crawl saw it (default for everything)
  * **confirmed** — the journey baseline has been approved (O0 lifecycle)
  * **client_declared** — a client-authored rule in the answer_key explicitly
    declares a field or expected value (O1 rule oracle)

Pure + dependency-free (unit-testable with plain dicts).  Tolerant: bad
inputs produce empty results, never crash.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

PROVENANCE_OBSERVED = "observed"
PROVENANCE_CONFIRMED = "confirmed"
PROVENANCE_CLIENT_DECLARED = "client_declared"

_CONFIRMED_STATUSES = frozenset({"approved", "validated"})


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name or "").strip().lower()).strip()


def extract_controls(
    page_state: Mapping[str, Any] | None,
    ledger_by_url: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Extract the control inventory from a single page state.

    Merges ``form_snapshot_signals`` (type, options, required, depends_on)
    with matching ``field_ledger`` entries (signature, semantic_type) by
    field name.  Returns one entry per distinct control.
    """
    if not isinstance(page_state, Mapping):
        return []

    signals = page_state.get("form_snapshot_signals") or {}
    if not isinstance(signals, Mapping):
        signals = {}

    url = str(page_state.get("location") or "")
    ledger_entries = (ledger_by_url or {}).get(url, []) if url else []
    ledger_by_name: dict[str, Mapping[str, Any]] = {}
    for entry in ledger_entries:
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or "").strip()
            if name:
                ledger_by_name[_normalize_name(name)] = entry

    controls: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for field_name, sig_data in signals.items():
        if not isinstance(sig_data, Mapping):
            continue
        name = str(field_name or "").strip()
        if not name:
            continue
        norm = _normalize_name(name)
        if norm in seen_names:
            continue
        seen_names.add(norm)

        ledger_match = ledger_by_name.get(norm, {})

        options = sig_data.get("options")
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options if str(o).strip()]

        entry: dict[str, Any] = {
            "name": name,
            "type": str(sig_data.get("type") or "text"),
            "options": options,
            "required": bool(sig_data.get("required")),
        }

        depends_on = sig_data.get("depends_on")
        if depends_on:
            entry["depends_on"] = str(depends_on)

        if isinstance(ledger_match, Mapping):
            sig = str(ledger_match.get("signature") or "")
            if sig:
                entry["signature"] = sig
            sem = str(ledger_match.get("semantic_type") or "")
            if sem:
                entry["semantic_type"] = sem
            if not options and isinstance(ledger_match.get("options"), list):
                entry["options"] = [
                    str(o) for o in ledger_match["options"]
                    if str(o).strip()]

        controls.append(entry)

    for norm_name, ledger_entry in ledger_by_name.items():
        if norm_name in seen_names:
            continue
        seen_names.add(norm_name)
        name = str(ledger_entry.get("name") or "").strip()
        if not name:
            continue
        options = ledger_entry.get("options")
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options if str(o).strip()]

        entry = {
            "name": name,
            "type": "text",
            "options": options,
            "required": False,
        }
        sig = str(ledger_entry.get("signature") or "")
        if sig:
            entry["signature"] = sig
        sem = str(ledger_entry.get("semantic_type") or "")
        if sem:
            entry["semantic_type"] = sem
        controls.append(entry)

    return controls


def extract_outcomes(
    page_state: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract displayed outcome values from a page state."""
    if not isinstance(page_state, Mapping):
        return []
    displayed = page_state.get("displayed_values")
    if not isinstance(displayed, list):
        return []

    outcomes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dv in displayed:
        if not isinstance(dv, Mapping):
            continue
        label = str(dv.get("label") or "").strip()
        if not label:
            continue
        key = _normalize_name(label)
        if key in seen:
            continue
        seen.add(key)

        entry: dict[str, Any] = {"label": label}
        selector = str(dv.get("selector") or "").strip()
        if selector:
            entry["selector"] = selector
        vt = str(dv.get("value_type") or "").strip()
        if vt:
            entry["value_type"] = vt
        outcomes.append(entry)

    return outcomes


def merge_controls(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge incoming controls into an existing inventory.

    Keyed by normalized name.  Incoming entries update existing ones
    (latest observation wins), new entries are appended.  Capped at 200
    to prevent unbounded growth.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for ctrl in (existing or []):
        if isinstance(ctrl, Mapping):
            name = str(ctrl.get("name") or "").strip()
            if name:
                by_name[_normalize_name(name)] = dict(ctrl)

    for ctrl in incoming:
        if not isinstance(ctrl, Mapping):
            continue
        name = str(ctrl.get("name") or "").strip()
        if not name:
            continue
        norm = _normalize_name(name)
        if norm in by_name:
            prev = by_name[norm]
            prev.update({k: v for k, v in ctrl.items() if v})
        else:
            by_name[norm] = dict(ctrl)

    return list(by_name.values())[:200]


def merge_outcomes(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge incoming outcome displays into existing, keyed by label."""
    by_label: dict[str, dict[str, Any]] = {}
    for out in (existing or []):
        if isinstance(out, Mapping):
            label = str(out.get("label") or "").strip()
            if label:
                by_label[_normalize_name(label)] = dict(out)

    for out in incoming:
        if not isinstance(out, Mapping):
            continue
        label = str(out.get("label") or "").strip()
        if not label:
            continue
        norm = _normalize_name(label)
        if norm in by_label:
            prev = by_label[norm]
            prev.update({k: v for k, v in out.items() if v})
        else:
            by_label[norm] = dict(out)

    return list(by_label.values())[:100]


def build_states_index(
    coverage: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Build a {fingerprint: page_state} lookup from coverage.states."""
    if not isinstance(coverage, Mapping):
        return {}
    states = coverage.get("states")
    if not isinstance(states, list):
        return {}
    index: dict[str, Mapping[str, Any]] = {}
    for state in states:
        if not isinstance(state, Mapping):
            continue
        fp = str(state.get("ax_fingerprint") or "").strip()
        if fp:
            index[fp] = state
    return index


def build_ledger_by_url(
    coverage: Mapping[str, Any] | None,
) -> dict[str, list[Mapping[str, Any]]]:
    """Build a {url: [ledger_entries]} lookup from coverage.field_ledger."""
    if not isinstance(coverage, Mapping):
        return {}
    ledger = coverage.get("field_ledger")
    if not isinstance(ledger, list):
        return {}
    by_url: dict[str, list[Mapping[str, Any]]] = {}
    for entry in ledger:
        if not isinstance(entry, Mapping):
            continue
        url = str(entry.get("url") or "").strip()
        if url:
            by_url.setdefault(url, []).append(entry)
    return by_url


def effective_provenance(
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> str:
    """The provenance badge for controls at a node, given the journey state.

    Returns the HIGHEST applicable provenance — used as the default for
    controls not individually overridden.  Individual controls that match
    a client-declared rule get ``client_declared`` regardless.
    """
    if baseline_status in _CONFIRMED_STATUSES:
        return PROVENANCE_CONFIRMED
    return PROVENANCE_OBSERVED


def apply_provenance(
    controls: list[dict[str, Any]],
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each control with its effective provenance badge.

    Mutates in place and returns the list for convenience.

    * A control whose name matches a client-authored rule field gets
      ``client_declared`` — the client explicitly declares it.
    * Otherwise, if the baseline is approved/validated → ``confirmed``.
    * Otherwise → ``observed``.
    """
    base = effective_provenance(baseline_status)
    norm_rules = frozenset(_normalize_name(f) for f in (rule_fields or []))
    for ctrl in controls:
        name = _normalize_name(str(ctrl.get("name") or ""))
        if name and name in norm_rules:
            ctrl["provenance"] = PROVENANCE_CLIENT_DECLARED
        else:
            ctrl["provenance"] = base
    return controls


def apply_outcome_provenance(
    outcomes: list[dict[str, Any]],
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each outcome with its effective provenance badge."""
    base = effective_provenance(baseline_status)
    norm_rules = frozenset(_normalize_name(f) for f in (rule_fields or []))
    for out in outcomes:
        label = _normalize_name(str(out.get("label") or ""))
        if label and label in norm_rules:
            out["provenance"] = PROVENANCE_CLIENT_DECLARED
        else:
            out["provenance"] = base
    return outcomes


def catalog_summary(
    nodes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute summary statistics for a catalog view."""
    total_controls = 0
    total_outcomes = 0
    controls_with_options = 0
    required_count = 0
    node_count = len(nodes)

    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        for ctrl in (node.get("controls") or []):
            if not isinstance(ctrl, Mapping):
                continue
            total_controls += 1
            if ctrl.get("options"):
                controls_with_options += 1
            if ctrl.get("required"):
                required_count += 1
        total_outcomes += len(node.get("displayed_outcomes") or [])

    return {
        "node_count": node_count,
        "total_controls": total_controls,
        "controls_with_options": controls_with_options,
        "required_controls": required_count,
        "total_outcomes": total_outcomes,
    }
