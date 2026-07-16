"""Data Agent — LLM refinement over the deterministic floor (Phase 3).

The Phase-1 six-disposition classifier is the deterministic FLOOR. The Data Agent asks
an LLM to refine it (classify novel fields, propose better defaults) but the LLM's own
claim is NEVER trusted — every proposal is RE-VALIDATED against the app's real captured
inventory (mirroring the proven brief_compiler ``ground_and_assemble`` pattern), and a
final non-bypassable HARD LINE guarantees a proposed value for a sensitive field (SSN,
account #, policy id, login) can never enter the crawl's fill.

Guarantees (all pure + adversarially tested — the doctrine's teeth for the data layer):
  * GROUNDING GATE — an LLM PICK is accepted only if its value is a VERBATIM observed
    option; an LLM SYNTHESIZE only if the value is format-valid for the field and the
    field is not sensitive. Anything ungrounded falls back to the floor with a reason.
  * HARD LINE — after grounding, any ASK field (or any label the classifier deems
    sensitive) has its value dropped and disposition forced to ASK. So even an LLM that
    fabricates ``123-45-6789`` for an SSN cannot get that value into the fill.
  * FLOOR-FIRST — with the LLM disabled/empty, the manifest is exactly the deterministic
    floor: usable, honest, and the LLM's marginal contribution is measurable
    (``autonomy_delta``), never assumed.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from .dispositions import (
    ASK, PICK, SYNTHESIZE, APPROVE, CARRY, OBSERVE,
    Disposition, FieldSignal, classify_field, is_sensitive_label, normalize_label,
    _first_grounded_option,
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Dispositions an LLM proposal may set. It can make a field MORE conservative (ASK) or
# propose a grounded value (PICK/SYNTHESIZE), but it can never invent OBSERVE/CARRY/
# APPROVE semantics — those are decided from real substrate/ownership, not opinion.
_LLM_ALLOWED = frozenset({ASK, PICK, SYNTHESIZE})


def _value_valid_for_type(value: str, ftype: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    t = (ftype or "text").strip().lower()
    if t in ("number", "range"):
        return bool(_NUM_RE.match(v))
    if t == "email":
        return bool(_EMAIL_RE.match(v))
    if t in ("date", "datetime-local", "month"):
        return bool(_DATE_RE.match(v[:10]))
    return True  # free text — any non-empty value is format-acceptable


def refine_with_llm(
    floor: list[Disposition],
    proposal: Iterable[Mapping[str, Any]] | None,
    inventory: Iterable[Mapping[str, Any]],
    *,
    today: date | None = None,
) -> tuple[list[Disposition], list[dict]]:
    """Apply a (tolerantly-parsed) LLM proposal to the floor UNDER the grounding gate.

    Returns ``(items, review)`` where ``review`` records every accept/reject with a
    machine-checkable reason. The LLM only ever REFINES; the floor is the fallback.
    """
    sig_by_label: dict[str, FieldSignal] = {}
    for f in inventory:
        lbl = str(f.get("label") or "")
        sig_by_label[normalize_label(lbl)] = FieldSignal(
            label=lbl, type=str(f.get("type") or "text"),
            options=tuple(str(o) for o in (f.get("options") or ())),
            required=bool(f.get("required")),
        )

    prop_by_label: dict[str, Mapping[str, Any]] = {}
    for p in (proposal or ()):
        lbl = str((p or {}).get("label") or "")
        if lbl:
            prop_by_label[normalize_label(lbl)] = p

    out: list[Disposition] = []
    review: list[dict] = []
    for item in floor:
        norm = normalize_label(item.label)
        p = prop_by_label.get(norm)
        if not p:
            out.append(item)
            continue
        want = str(p.get("disposition") or "").strip().upper()
        value = str(p.get("value") or "").strip()
        sig = sig_by_label.get(norm)

        if want not in _LLM_ALLOWED:
            out.append(item)
            review.append({"label": item.label, "action": "ignored",
                           "reason": f"LLM disposition {want!r} not permitted; floor kept"})
            continue

        # The LLM may make a field MORE conservative (→ ASK) at any time.
        if want == ASK:
            out.append(Disposition(
                label=item.label, disposition=ASK, default=None, grounded=False,
                reason="LLM flagged sensitive — asked, not filled",
                options=item.options, required=item.required,
            ))
            review.append({"label": item.label, "action": "accepted_ask",
                           "reason": "LLM escalated to ASK"})
            continue

        # A grounded PICK: the value must be a VERBATIM observed option.
        if want == PICK and sig is not None:
            observed = {o.strip() for o in sig.options}
            if value and value in observed:
                out.append(Disposition(
                    label=item.label, disposition=PICK, default=value, grounded=True,
                    reason="LLM PICK grounded to an observed option",
                    options=item.options, required=item.required,
                ))
                review.append({"label": item.label, "action": "accepted_pick", "reason": "value is an observed option"})
                continue
            out.append(item)
            review.append({"label": item.label, "action": "rejected_pick",
                           "reason": "proposed option was not observed on the page; floor kept"})
            continue

        # A grounded SYNTHESIZE: value must be type-valid AND the field non-sensitive.
        if want == SYNTHESIZE:
            ftype = sig.type if sig is not None else "text"
            if not is_sensitive_label(item.label) and _value_valid_for_type(value, ftype):
                out.append(Disposition(
                    label=item.label, disposition=SYNTHESIZE, default=value, grounded=True,
                    reason="LLM SYNTHESIZE value is format-valid for the field",
                    options=item.options, required=item.required,
                ))
                review.append({"label": item.label, "action": "accepted_synth", "reason": "format-valid value"})
                continue
            out.append(item)
            review.append({"label": item.label, "action": "rejected_synth",
                           "reason": "value invalid for type or field is sensitive; floor kept"})
            continue

        out.append(item)
        review.append({"label": item.label, "action": "ignored", "reason": "no grounded acceptance path"})

    return out, review


def enforce_ask_hardline(items: list[Disposition]) -> list[Disposition]:
    """THE HARD LINE — the final, non-bypassable pass.

    For any field whose disposition is ASK, OR whose label the classifier deems
    sensitive, drop any value and force disposition=ASK. Run LAST so no upstream
    reordering or LLM proposal can leak a fabricated real value into the fill.
    """
    guarded: list[Disposition] = []
    for it in items:
        if it.disposition == ASK or is_sensitive_label(it.label):
            if it.disposition == ASK and it.default is None:
                guarded.append(it)
            else:
                guarded.append(Disposition(
                    label=it.label, disposition=ASK, default=None, grounded=False,
                    reason="sensitive — must be human-provided (never invented)",
                    options=it.options, required=it.required,
                ))
        else:
            guarded.append(it)
    return guarded


def propose_dispositions(
    inventory: Iterable[Mapping[str, Any]],
    *,
    llm_proposal: Iterable[Mapping[str, Any]] | None = None,
    library_keys: Iterable[str] = (),
    observe_labels: Iterable[str] = (),
    submit_labels: Iterable[str] = (),
    today: date | None = None,
) -> dict:
    """Full Data-Agent pipeline: floor → LLM-refine (grounded) → HARD LINE.

    ``llm_proposal`` None ⇒ the deterministic floor alone (floor-first). Returns the
    guarded items plus ``review`` and an ``autonomy_delta`` isolating the LLM's effect.
    """
    inv = list(inventory)
    floor = [
        classify_field(
            FieldSignal(str(f.get("label") or ""), str(f.get("type") or "text"),
                        tuple(str(o) for o in (f.get("options") or ())), bool(f.get("required"))),
            library_keys=library_keys, observe_labels=observe_labels,
            submit_labels=submit_labels, today=today,
        )
        for f in inv
    ]
    refined, review = refine_with_llm(floor, llm_proposal, inv, today=today)
    guarded = enforce_ask_hardline(refined)
    return {
        "items": [g.as_dict() for g in guarded],
        "review": review,
        "autonomy_delta": autonomy_delta(floor, guarded),
        "llm_used": bool(llm_proposal),
    }


def autonomy_delta(floor: list[Disposition], refined: list[Disposition]) -> int:
    """How many fields the LLM MEASURABLY changed vs the deterministic floor — so the
    LLM's marginal value is a measured number, never assumed (if ~0 on real apps, the
    floor alone is enough and the LLM machinery can be descoped)."""
    fmap = {normalize_label(f.label): (f.disposition, f.default) for f in floor}
    changed = 0
    for r in refined:
        key = normalize_label(r.label)
        if key in fmap and fmap[key] != (r.disposition, r.default):
            changed += 1
    return changed


LLM_SYSTEM = (
    "You classify web form fields into dispositions for autonomous testing. "
    "Return ONLY a JSON array of objects {\"label\", \"disposition\", \"value\"}. "
    "disposition MUST be one of SYNTHESIZE, PICK, ASK. "
    "Use PICK only with a value copied VERBATIM from that field's listed options. "
    "Use ASK for any real identifier/credential you must never invent (SSN, account/"
    "policy/member id, login, card). Use SYNTHESIZE with a realistic value for safe "
    "fields (name, email, date, amount). Never invent an option or a secret."
)


def build_llm_prompt(inventory: Iterable[Mapping[str, Any]]) -> str:
    """A value-free prompt: labels/types/options only (no user-typed values)."""
    lines = ["Classify each field. Fields (label | type | options):"]
    for f in inventory:
        opts = ", ".join(str(o) for o in (f.get("options") or ())) or "-"
        lines.append(f"- {f.get('label')} | {f.get('type') or 'text'} | {opts}")
    lines.append('Respond with a JSON array of {"label","disposition","value"}.')
    return "\n".join(lines)


def parse_llm_proposal(text: str) -> list[dict]:
    """Tolerantly extract a ``[{label,disposition,value}]`` proposal from LLM text.

    Robust to prose around the JSON; returns [] on any parse failure (→ floor-only).
    The grounding gate + hard-line re-validate everything, so a malformed or hostile
    proposal can never produce an ungrounded or fabricated value.
    """
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out: list[dict] = []
    if isinstance(data, list):
        for d in data:
            if isinstance(d, dict) and str(d.get("label") or "").strip():
                out.append({
                    "label": str(d.get("label")),
                    "disposition": str(d.get("disposition") or ""),
                    "value": str(d.get("value") or ""),
                })
    return out


def fill_from_items(items: Iterable[Mapping[str, Any]]) -> dict:
    """Project the confirmed manifest into answer_key.fill — SYNTHESIZE/PICK/CARRY
    defaults only. OBSERVE (oracle) and ASK (never invented) are EXCLUDED, so a
    fabricated value can never reach the crawl even if one slipped through upstream."""
    fill: dict[str, str] = {}
    for it in items:
        disp = str(it.get("disposition") or "")
        default = it.get("default")
        if disp in (SYNTHESIZE, PICK, CARRY) and default not in (None, ""):
            fill[str(it.get("label"))] = str(default)
    return fill
