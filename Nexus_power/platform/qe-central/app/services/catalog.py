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

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

PROVENANCE_OBSERVED = "observed"
PROVENANCE_CONFIRMED = "confirmed"
PROVENANCE_CLIENT_DECLARED = "client_declared"

_CONFIRMED_STATUSES = frozenset({"approved", "validated"})

#: HTML constraint attributes that are pure validation shape (never user values).
_VALIDATION_KEYS = ("pattern", "minlength", "maxlength", "min", "max", "step")


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name or "").strip().lower()).strip()


def question_id_for(control: Mapping[str, Any]) -> str:
    """Stable, value-free question id (P2, Δ2).

    Prefers the control SIGNATURE — stable across crawls, so a re-crawl (which
    mints a fresh ``artifact_id``) still maps to the same catalog question and can
    be diffed against the last version. Falls back to the normalized accessible
    name when a control carries no signature. Never contains a user value.
    """
    basis = str(control.get("signature") or "").strip() or _normalize_name(
        str(control.get("name") or ""))
    return "q_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


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

        # Validation shape (P2) — HTML constraint attributes, never user values.
        validation = {}
        for vk in _VALIDATION_KEYS:
            vv = sig_data.get(vk)
            if vv not in (None, ""):
                validation[vk] = str(vv)[:80]
        if validation:
            entry["validation"] = validation

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

        entry["question_id"] = question_id_for(entry)
        controls.append(entry)

    # THE LEDGER FALLBACK IS KEYED BY URL, AND AN SPA SERVES MANY STATES FROM
    # ONE. It exists to catch a field the snapshot missed; on a five-step wizard
    # living at a single URL it instead attributed all five steps' fields to
    # every step. Live that listed most catalogue questions TWICE — once from
    # the state's own signals and once from the shared ledger, under two
    # different question_ids because one basis is the signature and the other
    # the name — and every fallback row claimed type "text" because a ledger
    # entry carries no control type. A client reading that catalogue sees each
    # question duplicated and half of them mistyped.
    #
    # A state that produced signals has already described itself. Only a state
    # with NO signals at all (the snapshot genuinely captured nothing) still
    # needs the ledger to speak for it.
    if not controls:
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
            entry["question_id"] = question_id_for(entry)
            controls.append(entry)

    return controls


#: Ceiling on the answers stored per catalogued question. Sized for the
#: enumerations business forms actually ask — 50 US states, ~250 countries, a
#: 100-year date-of-birth range. The previous 48 clipped the states themselves,
#: and the clip was silent: the catalogue simply held a shorter list, so every
#: case generated from it claimed to cover a question it only partly knew.
#: Bounded on purpose — one pathological control must not dominate a snapshot.
MAX_CATALOG_OPTIONS = 300


def _catalog_options(control: Mapping[str, Any]) -> list[str]:
    """The answer labels to store for one question, deduped and bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for o in (control.get("options") or []):
        label = str(o).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= MAX_CATALOG_OPTIONS:
            break
    return out


def _options_total(control: Mapping[str, Any]) -> int:
    """How many answers the question OFFERS, as observed in the page.

    Floored at the number actually stored so the catalogue can never claim fewer
    answers than it holds, and carried separately from the list so a clipped
    enumeration stays visible as clipped. A question whose stored options are a
    PREFIX is still a useful catalogue row; a prefix that PRESENTS as the whole
    answer set is a fabrication, and the difference is this number.
    """
    stored = len(_catalog_options(control))
    raw = control.get("options_total")
    try:
        declared = int(raw) if raw is not None and not isinstance(raw, bool) else 0
    except (TypeError, ValueError):
        declared = 0
    return max(declared, stored, len(control.get("options") or []))


def build_master_catalog(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]] | None = None,
    branches: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Aggregate per-node control inventories into ONE app-scoped Master Catalog.

    The per-node/per-journey catalog answers "what is on this page"; the Master
    Catalog answers "what questions does this APPLICATION have", deduped by stable
    ``question_id`` across every journey and node (Δ2) — the same question on two
    journeys is one row, not two, so the 400 questions are never duplicated per
    journey. Each row records every page it was seen on and keeps the richest
    metadata observed. ``expected_next_page`` is filled from the journey edges when
    available. Pure: a DB reader loads the nodes/edges/branches and calls this.

    ``nodes`` items: ``{node_fp|fingerprint, url, title, controls|controls_inventory}``.
    ``edges`` items: ``{from_fp, to_fp}`` (highest-priority next page per node).
    ``branches`` items: ``{node_fp, control_signature, control_label_norm,
    option_label_norm}`` — questionnaire questions (bare Yes/No etc.) live as
    branch rows, not form fields, so they are folded in here too. Their
    ``question_id`` is derived from the control SIGNATURE, the same id space the
    persona projector's trigger→child rules use — so P1 branch rules join the
    catalog cleanly.
    """
    next_by_node: dict[str, str] = {}
    page_by_fp: dict[str, str] = {}
    for e in (edges or []):
        if not isinstance(e, Mapping):
            continue
        frm, to = str(e.get("from_fp") or ""), str(e.get("to_fp") or "")
        if frm and to and frm not in next_by_node:
            next_by_node[frm] = to

    # PAGE IDENTITY IS THE URL, NOT THE TITLE. A single-page application serves
    # every step of a wizard under ONE <title>, so a title-first label collapsed
    # every question in the application onto one "page": live, all 24 catalogued
    # questions read as ["Summit Life Carrier Administration"]. The catalogue
    # could not say which step a question belonged to — the first thing anyone
    # asks of it — and "questions per step" was unrepresentable.
    #
    # Where one URL genuinely carries several distinct states (an SPA wizard is
    # exactly that), the state fingerprint disambiguates. Only where it must:
    # an ordinary page keeps a clean, legible URL as its label.
    url_counts: dict[str, int] = {}
    for node in nodes:
        if isinstance(node, Mapping):
            u = str(node.get("url") or "")
            if u:
                url_counts[u] = url_counts.get(u, 0) + 1

    by_qid: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_fp = str(node.get("node_fp") or node.get("fingerprint") or "")
        url = str(node.get("url") or "")
        if url:
            page = (url[:200] if url_counts.get(url, 0) < 2
                    else f"{url[:180]}#{node_fp[:12]}")
        else:
            page = str(node.get("title") or node_fp or "")[:200]
        if node_fp:
            page_by_fp[node_fp] = page
        controls = node.get("controls")
        if controls is None:
            controls = node.get("controls_inventory")
        if not isinstance(controls, Sequence) or isinstance(controls, (str, bytes)):
            continue
        for c in controls:
            if not isinstance(c, Mapping):
                continue
            qid = str(c.get("question_id") or "") or question_id_for(c)
            row = by_qid.get(qid)
            if row is None:
                row = {
                    "question_id": qid,
                    "name": str(c.get("name") or "")[:200],
                    "type": str(c.get("type") or "text"),
                    "options": _catalog_options(c),
                    # How many answers the question OFFERS. Differs from
                    # len(options) only when a read was clipped, and that
                    # difference is exactly what a consumer must be able to see.
                    "options_total": _options_total(c),
                    "required": bool(c.get("required")),
                    "semantic_type": str(c.get("semantic_type") or ""),
                    "provenance": str(c.get("provenance") or PROVENANCE_OBSERVED),
                    "pages": [],
                }
                val = c.get("validation")
                if isinstance(val, Mapping) and val:
                    row["validation"] = dict(val)
                nxt = next_by_node.get(node_fp)
                if nxt:
                    row["expected_next_page"] = nxt
                by_qid[qid] = row
            else:
                # Keep the richest observation: required is sticky-True, fill in
                # options/validation if this sighting has them and the row didn't.
                row["required"] = row["required"] or bool(c.get("required"))
                # RICHEST WINS. The same question is met on several pages and one
                # sighting may be more complete than another — a dependent
                # dropdown is empty until its driver is answered, so the first
                # sighting of "County" can legitimately offer nothing while a
                # later one offers forty. Keeping the first observation meant the
                # catalogue held the emptiest view of every dependent question.
                incoming = _catalog_options(c)
                if len(incoming) > len(row["options"]):
                    row["options"] = incoming
                row["options_total"] = max(
                    int(row.get("options_total") or 0), _options_total(c))
                if "validation" not in row and isinstance(c.get("validation"), Mapping):
                    if c["validation"]:
                        row["validation"] = dict(c["validation"])
            if page and page not in row["pages"]:
                row["pages"].append(page)

    # Fold in questionnaire questions (branch rows): a question per distinct
    # control signature, its options accumulated across its branch rows. Keyed by
    # ``question_id_for({"signature": ...})`` so the projector's rules join here.
    for b in (branches or []):
        if not isinstance(b, Mapping):
            continue
        sig = str(b.get("control_signature") or "")
        label = str(b.get("control_label_norm") or b.get("control_label") or "")
        if not sig and not label:
            continue
        qid = question_id_for({"signature": sig, "name": label})
        opt = str(b.get("option_label_norm") or b.get("option") or "")
        node_fp = str(b.get("node_fp") or "")
        row = by_qid.get(qid)
        if row is None:
            row = {
                "question_id": qid,
                "name": label[:200],
                "type": "choice",
                "options": [],
                "required": False,
                "semantic_type": "",
                "provenance": PROVENANCE_OBSERVED,
                "pages": [],
                "source": "branch",
            }
            nxt = next_by_node.get(node_fp)
            if nxt:
                row["expected_next_page"] = nxt
            by_qid[qid] = row
        if opt and opt not in row["options"]:
            row["options"].append(opt)
        page = page_by_fp.get(node_fp) or node_fp
        if page and page not in row["pages"]:
            row["pages"].append(page)

    questions = sorted(
        by_qid.values(), key=lambda r: (r.get("name") or "", r["question_id"]))
    summary = {
        "question_count": len(questions),
        "required_count": sum(1 for q in questions if q.get("required")),
        "with_options": sum(1 for q in questions if q.get("options")),
        "pages": sorted({p for q in questions for p in q.get("pages", [])}),
    }
    return {"questions": questions, "summary": summary}


def snapshot_catalog(master: Mapping[str, Any], artifact_id: str = "") -> dict[str, Any]:
    """A deterministic, hashable snapshot of a Master Catalog for versioning (P2)
    and regression diffing (P6).

    The hash is over each question's SHAPE — id, name, type, required, options,
    validation, business rule, expected next page — so a re-crawl that changed a
    question (new option, moved branch, tightened validation) produces a different
    hash, while a re-crawl that changed nothing reproduces it exactly.
    """
    questions = list(master.get("questions") or [])
    canon = json.dumps(
        [[q.get("question_id"), q.get("name"),
          q.get("answer_type") or q.get("type") or "text",
          bool(q.get("required")), sorted(str(o) for o in (q.get("options") or [])),
          q.get("validation") or {}, q.get("business_rule") or "",
          q.get("expected_next_page") or ""] for q in questions],
        sort_keys=True, separators=(",", ":"))
    return {
        "artifact_id": artifact_id,
        "question_count": len(questions),
        "snapshot_hash": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32],
        "questions": questions,
    }


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
