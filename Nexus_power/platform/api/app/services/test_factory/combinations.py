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

# Toggle / radio / segmented-control steps render as "Select '<option>'"
# (the option label, not the field label), so a combination override has to
# match them by the axis's OPTION set, not by the field name.
_SELECT_RX = re.compile(r"^Select '(.+?)'\s*$")

# Grounded FORM-FLOW steps (generator.generate_form_flow_journeys) render fills
# as "Select '<value>' in '<field>'" / "Enter '<value>' in '<field>'" — a third
# shape that must also be overridden, or a combination built on a form-flow
# base silently duplicates the demonstrated case and gets dropped (live: the
# quote flow produced option domains but zero combinations).
_FORMFLOW_FILL_RX = re.compile(r"^(Select|Enter) '(.+)' in '(.+)'$")


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
    #: Per-case structural risk score + 1-based rank (risk-descending) — persisted
    #: by the service into source_evidence so the RANKING survives to the client.
    risk_by_test_id: dict = field(default_factory=dict)
    rank_by_test_id: dict = field(default_factory=dict)


# Vision over-capture guards: dates aren't enums, nav-menus aren't form choices.
# Date detection is language-NEUTRAL first (ISO + numeric separators work in any
# locale); English month names are only an extra hint, never the sole signal.
_DATE_RX = re.compile(
    r"\b("
    r"\d{4}-\d{1,2}-\d{1,2}"                 # ISO 2026-06-12
    r"|\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}"    # 12/06/2026  12.06.26  6-12-2026
    r"|\d{1,2}[/.\-]\d{1,2}"                 # 12/06
    r"|(?:19|20)\d\d"                         # a year
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"  # English month hint only
    r")\b",
    re.IGNORECASE,
)
# English-convenience hint for nav-menu detection; the PRIMARY menu guard is the
# language-neutral average-phrase-length check in harvest_option_domains.
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


def _alnum(text: str) -> str:
    """Lower-cased alphanumerics only — joins a committed VALUE ('250000',
    'term') to its display-label option ('$250,000', 'VKPower Term')."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _committed_fills_by_label(page_actions: Iterable) -> dict[str, str]:
    """label(normalised) -> last committed fill value, from the DEMONSTRATED
    type/select actions. This is the crawl-substrate ground truth for 'the user
    actually selected a value here' — the qe-explorer form_snapshot carries
    options but no `selected` key, so without this join every captured domain
    fails the demonstrated-interaction bar (live: quote app, 6 domains, zero
    combinations)."""
    out: dict[str, str] = {}
    for a in page_actions or []:
        if (getattr(a, "verb", "") or "").strip().lower() in ("type", "select") \
                and str(getattr(a, "value", "") or "").strip() \
                and (getattr(a, "target_label", "") or "").strip():
            out[_norm(a.target_label)] = str(a.value).strip()
    return out


def _map_value_to_option(value: str, options: Sequence[str]) -> str:
    """Canonicalise a committed VALUE to its display-label option: exact
    alnum-lower equality first, else UNIQUE containment ('term' ->
    'VKPower Term'); ambiguity/none -> the committed value verbatim (still an
    honest demonstrated selection, just not label-canonical)."""
    v = _alnum(value)
    if not v:
        return value
    exact = [o for o in options if _alnum(o) == v]
    if len(exact) == 1:
        return exact[0]
    contains = [o for o in options if v and v in _alnum(o)]
    if len(contains) == 1:
        return contains[0]
    return value


def harvest_option_domains(
    page_visits: Iterable[PageVisitInput],
    page_actions: Iterable | None = None,
) -> list[OptionDomain]:
    """Collect fields that captured >= 2 distinct options (real choices).

    Source: ``form_snapshot_signals[label] = {selected, options[], required}``.
    Only fields whose options were actually observed become axes. When the
    signals carry no ``selected`` (the crawl-substrate shape), the demonstrated
    selection is joined from ``page_actions`` committed fills — grounded, never
    guessed.

    Quality guards against vision over-capture (a field is dropped, never
    guessed):
      * date fields read as enums (options that look like dates) — those are
        boundary tests, not combination axes;
      * navigation menus mis-read as choice controls (most options are action
        phrases like "Book a flight" / "Add to your trip");
      * semantically-duplicate axes (two labels with the SAME option set, e.g.
        "Flight" and "Trip Type" both = [Roundtrip, One-way]) — kept once.
    """
    committed = _committed_fills_by_label(page_actions)
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
            if not selected:
                fill = committed.get(_norm(clean), "")
                if fill:
                    selected = _map_value_to_option(fill, opts)
            # Principled axis bar: only fields the user ACTUALLY selected a
            # value in (demonstrated interaction) become combination axes.
            # Drops navigation menus the user merely hovered (no selection).
            if not selected:
                continue
            # Drop date-like and navigation-menu fields.
            if sum(_is_date_like(o) for o in opts) >= (len(opts) + 1) // 2:
                continue
            # Language-neutral menu guard: real enum values are short noun-like
            # tokens (Economy / Business, Male / Female, One-way / Roundtrip);
            # navigation items and sentences are long phrases. Average word count
            # separates them without any language-specific word list.
            avg_words = sum(len(o.split()) for o in opts) / max(1, len(opts))
            if avg_words >= 3.0:
                continue
            if sum(_is_action_phrase(o) for o in opts) >= 2:
                continue
            seen_labels.add(clean)
            candidates.append(OptionDomain(
                field_label=clean,
                selected=selected,
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
        # Per-option remaining-coverage weight: how many UNCOVERED pairs still
        # involve (axis, option). Without this, the FIRST axis placed in each
        # test has an empty partner set (gain 0 for every option) and always
        # falls back to opts[0] — so pairs involving its other options are
        # never covered, the loop stalls at max_guard, and dedup collapses the
        # output (live: a 4x4x3x2x2 space "covered" by 6 near-identical rows,
        # every one Product=first-option).
        involve: dict[tuple, int] = {}
        for pair_a, pair_b in uncovered:
            involve[pair_a] = involve.get(pair_a, 0) + 1
            involve[pair_b] = involve.get(pair_b, 0) + 1
        # Greedily build one test maximizing newly-covered pairs; ties broken
        # by remaining involvement so early-placed axes rotate their options.
        assignment: dict[int, str] = {}
        order = sorted(range(len(axes)), key=lambda k: -len(axes[k][1]))
        for idx in order:
            label, opts = axes[idx]
            best_opt = opts[0]
            best_key = (-1, -1)
            for opt in opts:
                gain = 0
                for other_idx, other_opt in assignment.items():
                    lo, hi = sorted([(idx, opt), (other_idx, other_opt)])
                    if (lo, hi) in uncovered:
                        gain += 1
                key = (gain, involve.get((idx, opt), 0))
                if key > best_key:
                    best_key = key
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
    """Clone the demonstrated base, overriding the axis fields' demonstrated value.

    A value can appear in the base in two shapes — both must be overridden, or
    the variant silently duplicates the demonstrated case:
      * a text/select FILL:  ``Enter '<value>' in the '<field>' field``
      * a toggle/radio PICK:  ``Select '<option>'``  (option label, not field)
    """
    norm_combo = {_norm(k): (k, v) for k, v in combo.items()}
    # Per axis: the normalized set of its captured options, to recognise the
    # toggle step that currently picks the demonstrated option.
    axis_options = {
        nlabel: {_norm(o) for o in (domains[label].options if label in domains else [])}
        for nlabel, (label, _v) in norm_combo.items()
    }
    steps: list[ProductionTestStep] = []
    for st in base.steps:
        new = st.model_copy(deep=True)
        action = (st.action or "")
        sel = _SELECT_RX.match(action.strip())
        flow = _FORMFLOW_FILL_RX.match(action.strip())
        for nlabel, (label, value) in norm_combo.items():
            if f"in the '{label}' field" in action:
                new.action = f"Enter '{value}' in the '{label}' field"
                new.expected = f"'{label}' shows '{value}'"
                new.expected_result = new.expected
                new.data_ref = value
                new.observed = {"verb": "type", "label": label, "kind": "field", "value": value}
                new.provenance = "available"  # captured option, not demonstrated
            elif flow and _norm(flow.group(3)) == nlabel:
                # Form-flow shape: keep the step's own verb/kind (dropdown vs
                # text) from the base's observed metadata — only the VALUE is a
                # different captured option.
                phrase = flow.group(1)
                new.action = f"{phrase} '{value}' in '{label}'"
                new.expected = f"'{label}' holds '{value}'"
                new.expected_result = new.expected
                new.data_ref = value
                obs = dict(getattr(st, "observed", None) or {})
                obs["value"] = value
                obs.setdefault("verb", "select" if phrase == "Select" else "type")
                obs.setdefault("label", label)
                new.observed = obs
                new.provenance = "available"
            elif sel and _norm(sel.group(1)) in axis_options.get(nlabel, set()):
                new.action = f"Select '{value}'"
                new.expected = f"'{value}' is selected"
                new.expected_result = new.expected
                new.observed = {"verb": "select", "label": value, "kind": "toggle"}
                new.provenance = "available"
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
    base_case: ProductionTestCase | None = None,
    base_cases: Sequence[ProductionTestCase] | None = None,
    page_visits: Iterable[PageVisitInput],
    page_actions: Iterable | None = None,
    host: str = "",
    max_active: int = _DEFAULT_MAX_ACTIVE,
) -> CombinationResult:
    """Generate the bounded, RISK-RANKED active combination suite + reserve spec.

    ``base_cases`` (preferred): combinations are built over EVERY demonstrated
    base flow — the primary E2E when one exists, plus each grounded form-flow
    (the business flows carrying real fill steps). One base per combination
    signature: bases are tried in the given order and a combo already emitted
    by an earlier base is not re-emitted by a later one (live: the quote app's
    flatten was suppressed, so the single-base wiring produced ZERO combinations
    despite 6 captured option domains). ``base_case`` is kept for back-compat.

    Ranking: pairwise combos are ordered by the structural risk score
    (required-field +2, non-demonstrated option +1), globally across bases; the
    1-based rank + score are returned per test_id and stamped into tags so the
    ordering survives to the client (Rank 1 = highest risk first).
    """
    visits = list(page_visits)
    domains = harvest_option_domains(visits, page_actions)
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

    bases = [b for b in (base_cases if base_cases is not None else [base_case])
             if b is not None]

    if not axes or not bases:
        return CombinationResult(
            active=[],
            option_domains=option_domains_json,
            generation_spec={
                "strategy": "pairwise",
                "axes": [{"field": l, "options": o} for l, o in axes],
                "full_count": 0,
                "selected_count": 0,
                "base_test_id": getattr(bases[0], "test_id", None) if bases else None,
                "base_test_ids": [b.test_id for b in bases],
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

    # Build across ALL bases; one case per combination signature (first base in
    # order wins — form-flow bases should be passed before nav-only bases).
    # Keep only variants that actually DIFFER from their demonstrated base: a
    # combo whose generated steps are identical to the base (values match what
    # the recording already showed) adds no coverage — drop it. Robust to a
    # noisy captured `selected` signal: the source of truth is the base case's
    # own steps, not the OCR-reported selection. Generic.
    scored: list[tuple[ProductionTestCase, int]] = []
    emitted_signatures: set[str] = set()
    for base in bases:
        base_sig = [s.action for s in (base.steps or [])]
        for combo in combos:
            # The all-demonstrated combo duplicates the base flow even when its
            # step TEXT differs (committed values 'term'/'250000' vs canonical
            # option labels 'VKPower Term'/'$250,000') — no added coverage.
            if all(_norm(v) == _norm(domain_map[k].selected)
                   for k, v in combo.items() if k in domain_map):
                continue
            desc = ", ".join(f"{k}={v}" for k, v in combo.items())
            if desc in emitted_signatures:
                continue
            case = _build_combo_case(base, combo, host, artifact_id, domain_map)
            if [s.action for s in case.steps] == base_sig:
                continue
            emitted_signatures.add(desc)
            scored.append((case, _risk(combo, domain_map)))

    # Global risk-descending rank across bases; bounded active suite.
    scored.sort(key=lambda cs: cs[1], reverse=True)
    scored = scored[:max_active]
    active: list[ProductionTestCase] = []
    risk_by_test_id: dict = {}
    rank_by_test_id: dict = {}
    for rank, (case, risk) in enumerate(scored, start=1):
        case.name = f"Rank {rank} — {case.name}"[:500]
        case.tags = list(case.tags or []) + [
            f"combination-rank:{rank}", f"combination-risk:{risk}"]
        risk_by_test_id[case.test_id] = risk
        rank_by_test_id[case.test_id] = rank
        active.append(case)

    spec = {
        "strategy": "pairwise",
        "axes": [{"field": l, "options": o} for l, o in axes],
        "full_count": full_count,
        "selected_count": len(active),
        "base_test_id": bases[0].test_id,
        "base_test_ids": [b.test_id for b in bases],
        "max_active": max_active,
        "ranking": "risk-desc (required +2, non-demonstrated option +1)",
    }

    return CombinationResult(
        active=active,
        option_domains=option_domains_json,
        generation_spec=spec,
        full_count=full_count,
        selected_count=len(active),
        risk_by_test_id=risk_by_test_id,
        rank_by_test_id=rank_by_test_id,
    )
