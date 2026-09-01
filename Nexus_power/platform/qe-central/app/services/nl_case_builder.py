"""O2 — NL Case Builder (natural-language → grounded case specification).

The user says: *"Create a test case for a 35-year-old female where premium = $40"*
and this module turns it into a structured, GROUNDED case spec:

  1. LLM extracts intent (journey hint, field values, expected outcomes)
  2. Grounding gate maps every field/value to the app's REAL catalog
  3. Enumerable controls become ``choice_overrides``; free-text becomes ``fill``
  4. Expected outcomes are verified against existing O1 rules
  5. Unverified outcomes are HONESTLY labelled, never silently promoted

Architecture mirrors ``brief_compiler``: LLM PROPOSES → grounding gate VALIDATES.
Pure + dependency-free (the LLM is an injected ``propose_fn`` seam, and every
other input is a plain dict/list).  The moat is not the LLM call — it is the
grounding gate that prevents hallucinated fields from ever reaching a case.

VERIFICATION STATUS on expected outcomes:

  * ``confirmed`` — an O1 rule matches and the expected value falls within
    the rule's tolerance/match.  Safe to assert without human review.
  * ``unverified`` — NO rule supports this expectation.  The assertion is
    generated but must be approved by an SME before it becomes a regression
    gate.  This is the HONEST position: we do not invent confidence.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .brief_compiler import match_label

# ── verification status vocabulary ───────────────────────────────────────────
VERIFIED_CONFIRMED = "confirmed"
VERIFIED_UNVERIFIED = "unverified"

# ── helpers ──────────────────────────────────────────────────────────────────

def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _s(label).lower()).strip()


# ── vocabulary extraction ────────────────────────────────────────────────────

def collect_vocabulary(
    journey_summaries: Sequence[Mapping[str, Any]],
) -> dict:
    """Build a unified, deduplicated vocabulary from journey catalog summaries.

    Each journey_summary carries:
      * ``journey_id``, ``business_name``
      * ``controls``: ``[{name, type, options, signature}]``
      * ``outcomes``: ``[{label, selector, value_type}]``

    Returns ``{control_labels, controls, outcome_labels, outcomes,
    journey_names}`` — the vocabulary the LLM prompt is constrained to.
    """
    seen_controls: dict[str, dict] = {}
    seen_outcomes: dict[str, dict] = {}

    for js in (journey_summaries or []):
        for c in (js.get("controls") or []):
            name = _s(c.get("name")).strip()
            if not name:
                continue
            key = _norm(name)
            if key and key not in seen_controls:
                seen_controls[key] = {
                    "name": name,
                    "type": _s(c.get("type")),
                    "options": list(c.get("options") or []),
                    "signature": _s(c.get("signature")),
                }
        for o in (js.get("outcomes") or []):
            label = _s(o.get("label")).strip()
            if not label:
                continue
            key = _norm(label)
            if key and key not in seen_outcomes:
                seen_outcomes[key] = {
                    "label": label,
                    "selector": _s(o.get("selector")),
                    "value_type": _s(o.get("value_type")),
                }

    return {
        "control_labels": [v["name"] for v in seen_controls.values()],
        "controls": seen_controls,
        "outcome_labels": [v["label"] for v in seen_outcomes.values()],
        "outcomes": seen_outcomes,
        "journey_names": [
            _s(js.get("business_name")).strip()
            for js in (journey_summaries or [])
            if _s(js.get("business_name")).strip()
        ],
    }


# ── LLM prompt ───────────────────────────────────────────────────────────────

NL_SYSTEM_INSTRUCTION = (
    "You parse a QA analyst's natural-language test-case request into a STRICT JSON "
    "structure.  You NEVER invent field names.  You may ONLY use names from the "
    "provided KNOWN_CONTROLS and KNOWN_OUTCOMES lists, verbatim.  Output ONLY JSON, "
    "no prose."
)


def build_nl_prompt(
    *, nl_text: str, vocabulary: Mapping[str, Any],
) -> str:
    """Construct the grounded NL-parse prompt.

    Gives the LLM the app's real vocabulary and asks it to map the user's
    natural language to that vocabulary.  Unrecognised terms go into
    ``unmatched``.  The GROUNDING is re-checked in code — the prompt is a
    hint, :func:`ground_nl_intent` is the enforcement.
    """
    controls = vocabulary.get("controls") or {}
    control_lines = []
    for info in controls.values():
        opts = info.get("options") or []
        opt_str = f"  options: {opts}" if opts else ""
        control_lines.append(f"  - {info['name']} (type: {info.get('type', '?')}){opt_str}")

    outcome_lines = [
        f"  - {info['label']} (type: {info.get('value_type', '?')})"
        for info in (vocabulary.get("outcomes") or {}).values()
    ]

    journey_lines = [
        f"  - {name}" for name in (vocabulary.get("journey_names") or [])
    ]

    return (
        f"{NL_SYSTEM_INSTRUCTION}\n\n"
        f"JOURNEYS (the business flows this app has):\n"
        + ("\n".join(journey_lines) or "  (none discovered)") + "\n\n"
        f"KNOWN_CONTROLS (the ONLY field names you may use, verbatim):\n"
        + ("\n".join(control_lines) or "  (none captured)") + "\n\n"
        f"KNOWN_OUTCOMES (displayed output fields you may target):\n"
        + ("\n".join(outcome_lines) or "  (none captured)") + "\n\n"
        f"USER REQUEST:\n{_s(nl_text).strip()}\n\n"
        "Return JSON of this exact shape:\n"
        "{\n"
        '  "journey_hint": "<best-matching JOURNEY name, or empty if unclear>",\n'
        '  "fields": [\n'
        '    {"label": "<KNOWN_CONTROL name>", "value": "<the value from the request>"}\n'
        "  ],\n"
        '  "expected_outcomes": [\n'
        '    {"label": "<KNOWN_OUTCOME name>", "expected": <number|string>, '
        '"match": "numeric|exact", "tolerance": <number|null>}\n'
        "  ],\n"
        '  "unmatched": [{"text": "<phrase>", "reason": "<why>"}]\n'
        "}\n"
    )


# ── proposal parsing ─────────────────────────────────────────────────────────

def parse_nl_proposal(raw: Any) -> dict:
    """Tolerantly parse the LLM output into a structured intent.

    Same tolerance as ``brief_compiler.parse_proposal``: accepts a dict or a
    string (extracts the first JSON object).  Never raises — a malformed
    response yields empty lists.
    """
    if isinstance(raw, Mapping):
        obj = dict(raw)
    else:
        text = _s(raw).strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            try:
                obj = json.loads(m.group(0)) if m else {}
            except Exception:
                obj = {}
    if not isinstance(obj, Mapping):
        obj = {}
    return {
        "journey_hint": _s(obj.get("journey_hint")).strip(),
        "fields": list(obj.get("fields") or []),
        "expected_outcomes": list(obj.get("expected_outcomes") or []),
        "unmatched": list(obj.get("unmatched") or []),
    }


# ── journey matching ─────────────────────────────────────────────────────────

def match_journey(
    journey_hint: str,
    journey_summaries: Sequence[Mapping[str, Any]],
) -> dict | None:
    """Find the best matching journey by business_name.

    Uses ``match_label`` for tolerant matching (exact → token-subset →
    substring).  Returns the matched journey summary or ``None``.
    """
    if not journey_hint or not journey_summaries:
        return None
    names = [_s(js.get("business_name")).strip() for js in journey_summaries]
    matched_name = match_label(journey_hint, names)
    if matched_name is None:
        return None
    for js in journey_summaries:
        if _s(js.get("business_name")).strip() == matched_name:
            return dict(js)
    return None


def match_journey_by_fields(
    grounded_fields: Sequence[Mapping[str, Any]],
    journey_summaries: Sequence[Mapping[str, Any]],
) -> dict | None:
    """When the LLM gives no journey_hint, find the journey whose catalog
    has the most overlap with the grounded fields.  Tie-breaks by journey
    order (first discovered = default).
    """
    if not grounded_fields or not journey_summaries:
        return None
    grounded_names = {_norm(f.get("grounded_label", "")) for f in grounded_fields}
    grounded_names.discard("")
    if not grounded_names:
        return None

    best: dict | None = None
    best_score = 0
    for js in journey_summaries:
        control_names = {
            _norm(c.get("name", ""))
            for c in (js.get("controls") or [])
        }
        control_names.discard("")
        overlap = len(grounded_names & control_names)
        if overlap > best_score:
            best_score = overlap
            best = dict(js)
    return best


# ── option matching ──────────────────────────────────────────────────────────

def _match_option(value: str, options: Sequence[str]) -> str | None:
    """Match a user-provided value to a control's real option (tolerant)."""
    v = _norm(value)
    if not v or not options:
        return None
    for opt in options:
        if _norm(opt) == v:
            return opt
    for opt in options:
        no = _norm(opt)
        if v in no or no in v:
            return opt
    return None


# ── grounding gate ───────────────────────────────────────────────────────────

def ground_nl_intent(
    proposal: Mapping[str, Any],
    vocabulary: Mapping[str, Any],
    journey_summaries: Sequence[Mapping[str, Any]],
) -> dict:
    """Re-validate an (untrusted) LLM proposal against the app's real vocabulary.

    Returns ``{journey, fields, expected_outcomes, choice_overrides, fill,
    review, grounded, ungrounded}``.

    * ``journey`` — the matched journey summary (or ``None``).
    * ``fields`` — grounded field→value pairs with provenance.
    * ``choice_overrides`` — ``{control_signature: option_label_norm}`` for
      enumerable controls whose value matched an observed option.
    * ``fill`` — ``{control_name: value}`` for free-text controls.
    * ``expected_outcomes`` — outcome expectations (verification added later).
    """
    control_labels = list(vocabulary.get("control_labels") or [])
    outcome_labels = list(vocabulary.get("outcome_labels") or [])
    controls_map = dict(vocabulary.get("controls") or {})

    review: list[dict] = []
    grounded_fields: list[dict] = []
    choice_overrides: dict[str, str] = {}
    fill: dict[str, str] = {}
    expected_outcomes: list[dict] = []

    def _note(kind, field, grounded, reason, **extra):
        review.append({
            "kind": kind, "field": _s(field), "grounded": grounded,
            "needs_confirmation": not grounded, "reason": reason, **extra,
        })

    # ── journey ──────────────────────────────────────────────────────────
    journey_hint = _s(proposal.get("journey_hint"))
    journey = match_journey(journey_hint, journey_summaries) if journey_hint else None
    if journey_hint and journey:
        _note("journey", journey.get("business_name", ""), True,
              f"matched journey '{journey.get('business_name')}'")
    elif journey_hint:
        _note("journey", journey_hint, False,
              f"'{journey_hint}' does not match any discovered journey")

    # ── fields → choice_overrides or fill ────────────────────────────────
    for item in (proposal.get("fields") or []):
        if not isinstance(item, Mapping):
            continue
        proposed = _s(item.get("label") or item.get("field") or item.get("name"))
        value = _s(item.get("value"))
        real = match_label(proposed, control_labels)
        if real is None:
            _note("field", proposed, False,
                  f"'{proposed}' does not match any captured control", value=value)
            continue

        grounded_fields.append({"grounded_label": real, "value": value})
        control_info = controls_map.get(_norm(real))
        if not control_info:
            fill[real] = value
            _note("field", real, True, f"matched control '{real}' (free-text)", value=value)
            continue

        options = control_info.get("options") or []
        sig = control_info.get("signature", "")
        if options and sig:
            matched_opt = _match_option(value, options)
            if matched_opt is not None:
                choice_overrides[sig] = _norm(matched_opt)
                _note("field", real, True,
                      f"matched control '{real}' option '{matched_opt}'",
                      value=value, override=True)
            else:
                fill[real] = value
                _note("field", real, True,
                      f"matched control '{real}' but value '{value}' is not a "
                      f"known option ({options}); treated as fill",
                      value=value, override=False)
        else:
            fill[real] = value
            _note("field", real, True,
                  f"matched control '{real}' (free-text)", value=value)

    # ── expected outcomes ────────────────────────────────────────────────
    for item in (proposal.get("expected_outcomes") or []):
        if not isinstance(item, Mapping):
            continue
        proposed = _s(item.get("label") or item.get("field") or item.get("name"))
        real = match_label(proposed, outcome_labels)
        if real is not None:
            expected_outcomes.append({
                "field": real,
                "expected": item.get("expected"),
                "match": _s(item.get("match")) or "numeric",
                "tolerance": item.get("tolerance"),
            })
            _note("outcome", real, True,
                  f"matched outcome '{real}'", expected=item.get("expected"))
        else:
            expected_outcomes.append({
                "field": proposed,
                "expected": item.get("expected"),
                "match": _s(item.get("match")) or "numeric",
                "tolerance": item.get("tolerance"),
            })
            _note("outcome", proposed, False,
                  f"'{proposed}' does not match any captured outcome",
                  expected=item.get("expected"))

    # ── unmatched ────────────────────────────────────────────────────────
    for um in (proposal.get("unmatched") or []):
        if isinstance(um, Mapping):
            _note("unmatched", um.get("text", ""), False,
                  _s(um.get("reason")) or "the model could not map this")

    # ── auto-match journey by field overlap if LLM didn't pick one ──────
    if journey is None and grounded_fields:
        journey = match_journey_by_fields(grounded_fields, journey_summaries)
        if journey:
            _note("journey", journey.get("business_name", ""), True,
                  "auto-matched by field overlap (no explicit journey hint)")

    grounded_n = sum(1 for r in review if r["grounded"])
    return {
        "journey": journey,
        "fields": grounded_fields,
        "choice_overrides": choice_overrides,
        "fill": fill,
        "expected_outcomes": expected_outcomes,
        "review": review,
        "grounded": grounded_n,
        "ungrounded": len(review) - grounded_n,
    }


# ── outcome verification against O1 rules ────────────────────────────────────

def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _s(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[,\s$%€£]", "", text)
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(m.group(0)) if m else None


def verify_outcomes(
    expected_outcomes: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]] | None,
) -> list[dict]:
    """Check each expected outcome against existing O1 rules.

    Returns the outcomes with a ``verification`` status:
      * ``confirmed`` — a rule covers this expectation.
      * ``unverified`` — no rule supports it; the assertion needs SME approval.
    """
    rule_list = list(rules or [])
    rule_by_field: dict[str, list[dict]] = {}
    for r in rule_list:
        if not isinstance(r, Mapping):
            continue
        kind = _s(r.get("kind") or r.get("type")).strip().lower()
        if kind != "outcome_rule":
            continue
        field = _norm(r.get("field", ""))
        if field:
            rule_by_field.setdefault(field, []).append(dict(r))

    verified: list[dict] = []
    for eo in expected_outcomes:
        if not isinstance(eo, Mapping):
            continue
        field = _s(eo.get("field"))
        expected = eo.get("expected")
        match_type = _s(eo.get("match")) or "numeric"
        tolerance = eo.get("tolerance")

        status = VERIFIED_UNVERIFIED
        rule_source = None

        field_rules = rule_by_field.get(_norm(field), [])
        for rule in field_rules:
            if _rule_covers(rule, expected, match_type, tolerance):
                status = VERIFIED_CONFIRMED
                rule_source = _s(rule.get("source")) or None
                break

        verified.append({
            "field": field,
            "expected": expected,
            "match": match_type,
            "tolerance": tolerance,
            "verification": status,
            "rule_source": rule_source,
        })
    return verified


def _rule_covers(
    rule: Mapping[str, Any], expected: Any, match_type: str,
    user_tolerance: Any,
) -> bool:
    """True when the rule's expected value covers the user's expectation."""
    rule_expected = rule.get("expected")
    rule_match = _s(rule.get("match")).strip().lower() or "numeric"

    if rule_match == "numeric" or match_type == "numeric":
        re_num = _as_number(rule_expected)
        ue_num = _as_number(expected)
        if re_num is None or ue_num is None:
            return False
        tol = _as_number(rule.get("tolerance"))
        if tol is None:
            tol = _as_number(user_tolerance)
        if tol is None:
            tol = 0.0
        return abs(re_num - ue_num) <= tol

    if rule_match == "exact":
        return _norm(rule_expected) == _norm(expected)

    if rule_match == "contains":
        return _norm(rule_expected) in _norm(expected) or _norm(expected) in _norm(rule_expected)

    return _norm(rule_expected) == _norm(expected)


# ── case assembly ────────────────────────────────────────────────────────────

def assemble_case(
    *, nl_text: str, grounded: Mapping[str, Any],
    verified_outcomes: Sequence[Mapping[str, Any]],
) -> dict:
    """Assemble the final case specification.

    The case spec is the contract a downstream dispatch/test-case-generator
    consumes: it names the journey, the fill, the choice overrides, and the
    expected outcomes with honest verification status.
    """
    journey = grounded.get("journey") or {}
    confirmed = sum(
        1 for v in verified_outcomes if v.get("verification") == VERIFIED_CONFIRMED
    )
    unverified = sum(
        1 for v in verified_outcomes if v.get("verification") == VERIFIED_UNVERIFIED
    )

    return {
        "nl_text": _s(nl_text).strip(),
        "journey_id": _s(journey.get("journey_id")),
        "journey_name": _s(journey.get("business_name")),
        "choice_overrides": dict(grounded.get("choice_overrides") or {}),
        "fill": dict(grounded.get("fill") or {}),
        "expected_outcomes": list(verified_outcomes),
        "review": list(grounded.get("review") or []),
        "grounded": grounded.get("grounded", 0),
        "ungrounded": grounded.get("ungrounded", 0),
        "outcomes_confirmed": confirmed,
        "outcomes_unverified": unverified,
    }


# ── top-level orchestrator ───────────────────────────────────────────────────

def compile_nl_case(
    *, nl_text: str,
    journey_summaries: Sequence[Mapping[str, Any]],
    rules: Sequence[Mapping[str, Any]] | None = None,
    propose_fn: Callable[[str], Any] | None = None,
) -> dict:
    """Compile a natural-language request into a grounded case specification.

    ``propose_fn(prompt) → str|dict`` performs the LLM inference (injected —
    tests pass a fake).  When ``None`` or failing, the compiler degrades
    HONESTLY: it proposes nothing and reports the reason.

    Returns ``{case, prompt, llm_error}`` — the case spec, the prompt that
    was sent, and any LLM error message.
    """
    vocabulary = collect_vocabulary(journey_summaries)
    prompt = build_nl_prompt(nl_text=nl_text, vocabulary=vocabulary)

    llm_error = ""
    raw: Any = {}
    if propose_fn is not None:
        try:
            raw = propose_fn(prompt)
        except Exception as exc:
            llm_error = f"LLM proposal failed: {exc}"[:300]
    else:
        llm_error = "no LLM configured — manual case authoring required"

    proposal = parse_nl_proposal(raw)
    grounded = ground_nl_intent(proposal, vocabulary, journey_summaries)
    verified_outcomes = verify_outcomes(
        grounded["expected_outcomes"], rules,
    )
    case = assemble_case(
        nl_text=nl_text, grounded=grounded,
        verified_outcomes=verified_outcomes,
    )
    case["prompt"] = prompt
    case["llm_error"] = llm_error
    return case


__all__ = [
    "collect_vocabulary",
    "build_nl_prompt",
    "parse_nl_proposal",
    "match_journey",
    "match_journey_by_fields",
    "ground_nl_intent",
    "verify_outcomes",
    "assemble_case",
    "compile_nl_case",
    "VERIFIED_CONFIRMED",
    "VERIFIED_UNVERIFIED",
]
