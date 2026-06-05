"""Critical-combination generator (Phase 2).

Builds *available-option* combination test cases from the captured option
domains, grounded — never assumed:

    A combination value is allowed ONLY if it appears in a field's captured
    option list (``form_snapshot_signals[label].options``).  If the recording
    never revealed a field's options, that field is not an axis — no guessing.

Strategy: **pairwise** coverage over the option domains (every pair of
field-values covered at least once) + **risk weighting** (required fields and
non-default options rank higher), capped to a bounded active suite.  The full
option space + spec are stored in the reserve so any combination can be
reconstructed later.

Each combination clones the demonstrated base ``ProductionTestCase`` and
overrides only the axis fields' fill steps — navigation and assertions are
preserved.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from itertools import combinations as _itercombos
from typing import Iterable, Mapping, Sequence

from nexus_sdk.models import ProductionTestCase, ProductionTestStep

from .generator import PageVisitInput, _norm  # reuse normalization

_TEST_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")
_DEFAULT_MAX_ACTIVE = 40


@dataclass
class OptionDomain:
    field_label: str
    selected: str
    options: list[str]
    source: str  # "signals"
    required: bool = False


@dataclass
class CombinationResult:
    active: list[ProductionTestCase]
    option_domains: dict[str, dict]
    generation_spec: dict
    full_count: int
    selected_count: int


# Vision over-capture guards: dates aren't enums, nav-menus aren't form choices.
_DATE_RX = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d\d|\d{1,2}/\d{1,2})\b",
    re.IGNORECASE,
)
_ACTION_VERBS = frozenset({
    "book", "add", "find", "manage", "view", "see", "learn", "install", "sign",
    "go", "start", "explore", "get", "check", "open", "browse", "shop",
})


def _is_date_like(opt: str) -> bool:
    return bool(_DATE_RX.search(opt))


def _is_action_phrase(opt: str) -> bool:
    words = opt.strip().split()
    return len(words) >= 2 and words[0].lower().rstrip(".,") in _ACTION_VERBS


def _opt_key(options: Sequence[str]) -> frozenset:
    return frozenset(_norm(o) for o in options)


def harvest_option_domains(page_visits: Iterable[PageVisitInput]) -> list[OptionDomain]:
    """Collect fields that captured >= 2 distinct options (real choices).

    Source: ``form_snapshot_signals[label] = {selected, options[], required}``.
    Only fields whose options were actually observed become axes.

    Quality guards against vision over-capture (a field is dropped, never
    guessed):
      * date fields read as enums (options that look like dates) — those are
        boundary tests, not combination axes;
      * navigation menus mis-read as choice controls (most options are action
        phrases like "Book a flight" / "Add to your trip");
      * semantically-duplicate axes (two labels with the SAME option set, e.g.
        "Flight" and "Trip Type" both = [Roundtrip, One-way]) — kept once.
    """
    candidates: list[OptionDomain] = []
    seen_labels: set[str] = set()
    for visit in page_visits:
        signals = getattr(visit, "form_snapshot_signals", None) or {}
        for label, meta in signals.items():
            if not isinstance(meta, Mapping):
                continue
            clean = label.strip()
            if not clean or clean in seen_labels:
                continue
            opts = list(dict.fromkeys(
                str(o).strip() for o in (meta.get("options") or []) if str(o).strip()
            ))
            if len(opts) < 2:
                continue
            selected = str(meta.get("selected") or "").strip()
            # Principled axis bar: only fields the user ACTUALLY selected a
            # value in (demonstrated interaction) become combination axes.
            # Drops navigation menus the user merely hovered (no selection).
            if not selected:
                continue
            # Drop date-like and navigation-menu fields.
            if sum(_is_date_like(o) for o in opts) >= (len(opts) + 1) // 2:
                continue
            if sum(_is_action_phrase(o) for o in opts) >= 2:
                continue
            seen_labels.add(clean)
            candidates.append(OptionDomain(
                field_label=clean,
                selected=str(meta.get("selected") or "").strip(),
                options=opts,
                source="signals",
                required=bool(meta.get("required")),
            ))

    # De-duplicate semantically-identical axes (same option set) — keep the
    # most descriptive label (more words, then longer).
    by_set: dict[frozenset, OptionDomain] = {}
    for dom in candidates:
        key = _opt_key(dom.options)
        existing = by_set.get(key)
        if existing is None:
            by_set[key] = dom
            continue
        better = max(
            (existing, dom),
            key=lambda d: (len(d.field_label.split()), len(d.field_label)),
        )
        by_set[key] = better
    return list(by_set.values())


def _pairwise(axes: Sequence[tuple[str, list[str]]]) -> list[dict[str, str]]:
    """Greedy all-pairs (pairwise) covering set over the given axes.

    Each axis = (label, options).  Returns a list of assignments
    {label: option} such that every (axis_i=opt, axis_j=opt) pair for i<j is
    covered by at least one assignment.
    """
    if not axes:
        return []
    if len(axes) == 1:
        label, opts = axes[0]
        return [{label: o} for o in opts]

    # All pairs that must be covered.
    uncovered: set[tuple] = set()
    for (i, (_li, oi)), (j, (_lj, oj)) in _itercombos(enumerate(axes), 2):
        for a in oi:
            for b in oj:
                uncovered.add(((i, a), (j, b)))

    tests: list[dict[str, str]] = []
    guard = 0
    max_guard = sum(len(o) for _l, o in axes) * len(axes) * 4 + 16
    while uncovered and guard < max_guard:
        guard += 1
        # Greedily build one test maximizing newly-covered pairs.
        assignment: dict[int, str] = {}
        order = sorted(range(len(axes)), key=lambda k: -len(axes[k][1]))
        for idx in order:
            label, opts = axes[idx]
            best_opt = opts[0]
            best_gain = -1
            for opt in opts:
                gain = 0
                for other_idx, other_opt in assignment.items():
                    lo, hi = sorted([(idx, opt), (other_idx, other_opt)])
                    if (lo, hi) in uncovered:
                        gain += 1
                if gain > best_gain:
                    best_gain = gain
                    best_opt = opt
            assignment[idx] = best_opt
        for (i, a), (j, b) in list(uncovered):
            if assignment.get(i) == a and assignment.get(j) == b:
                uncovered.discard(((i, a), (j, b)))
        tests.append({axes[k][0]: v for k, v in assignment.items()})

    # De-dup identical assignments.
    seen: set[tuple] = set()
    out: list[dict[str, str]] = []
    for t in tests:
        key = tuple(sorted(t.items()))
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _risk(combo: Mapping[str, str], domains: Mapping[str, OptionDomain]) -> int:
    """Higher = more critical: required fields + non-default option choices."""
    score = 0
    for label, value in combo.items():
        dom = domains.get(label)
        if not dom:
            continue
        if dom.required:
            score += 2
        if _norm(value) != _norm(dom.selected):  # exercises a NON-demonstrated option
            score += 1
    return score


def _build_combo_case(
    base: ProductionTestCase, combo: Mapping[str, str], host: str,
    artifact_id: str, domains: Mapping[str, OptionDomain],
) -> ProductionTestCase:
    """Clone the demonstrated base, overriding only the axis fields' fills."""
    norm_combo = {_norm(k): (k, v) for k, v in combo.items()}
    steps: list[ProductionTestStep] = []
    for st in base.steps:
        new = st.model_copy(deep=True)
        action = st.action or ""
        for nlabel, (label, value) in norm_combo.items():
            if f"in the '{label}' field" in action:
                new.action = f"Enter '{value}' in the '{label}' field"
                new.expected = f"'{label}' shows '{value}'"
                new.expected_result = new.expected
                new.data_ref = value
        steps.append(new)

    desc_combo = ", ".join(f"{k}={v}" for k, v in combo.items())
    name = f"Combination: {desc_combo} ({host})"[:500]
    test_id = str(uuid.uuid5(
        _TEST_ID_NAMESPACE, f"{artifact_id}:combination:{desc_combo}",
    ))
    return ProductionTestCase(
        test_id=test_id,
        name=name,
        description=(
            f"Critical-combination variant of the demonstrated flow with "
            f"{desc_combo}. Each value is a captured available option for its "
            f"field (grounded as available, not demonstrated)."
        ),
        steps=steps,
        preconditions=list(base.preconditions or []),
        priority="P1_high",
        type="combination",
        tags=["combination", "available", "e2e", "pages_and_forms"],
    )


def generate_combination_cases(
    *,
    artifact_id: str,
    base_case: ProductionTestCase | None,
    page_visits: Iterable[PageVisitInput],
    host: str = "",
    max_active: int = _DEFAULT_MAX_ACTIVE,
) -> CombinationResult:
    """Generate the bounded active combination suite + the reserve spec."""
    visits = list(page_visits)
    domains = harvest_option_domains(visits)
    axes = [(d.field_label, d.options) for d in domains]
    domain_map = {d.field_label: d for d in domains}

    option_domains_json = {
        d.field_label: {
            "selected": d.selected,
            "options": d.options,
            "source": d.source,
            "required": d.required,
        }
        for d in domains
    }

    if not axes or base_case is None:
        return CombinationResult(
            active=[],
            option_domains=option_domains_json,
            generation_spec={
                "strategy": "pairwise",
                "axes": [{"field": l, "options": o} for l, o in axes],
                "full_count": 0,
                "selected_count": 0,
                "base_test_id": getattr(base_case, "test_id", None),
                "note": "no captured option domains (>=2 options) — no combinations",
            },
            full_count=0,
            selected_count=0,
        )

    full_count = 1
    for _l, opts in axes:
        full_count *= len(opts)

    combos = _pairwise(axes)
    combos.sort(key=lambda c: _risk(c, domain_map), reverse=True)
    selected = combos[:max_active]

    active = [
        _build_combo_case(base_case, combo, host, artifact_id, domain_map)
        for combo in selected
    ]

    spec = {
        "strategy": "pairwise",
        "axes": [{"field": l, "options": o} for l, o in axes],
        "full_count": full_count,
        "selected_count": len(active),
        "base_test_id": base_case.test_id,
        "max_active": max_active,
    }

    return CombinationResult(
        active=active,
        option_domains=option_domains_json,
        generation_spec=spec,
        full_count=full_count,
        selected_count=len(active),
    )
