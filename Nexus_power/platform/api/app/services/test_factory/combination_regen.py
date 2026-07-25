"""V3 — combination regeneration: the honest repair for un-healable axes.

The class (diagnosed live, 2026-07-25, scenario 31d06925 step 9): a
combination case sets an option-captured axis (Relationship=Child) on a field
that DOES NOT EXIST in the form state its other substituted values produce
(conditional field — the app only renders it on the demonstrated path). No
locator heal can conjure a missing field, and the auto-heal driver correctly
refuses (``no_grounded_fix``). The truthful repair is REGENERATION: drop the
absent axis's step, so the case matches the form the app actually presents —
with the coverage reduction LABELED, never hidden.

Honesty rails (each pinned by tests):
  * only an ``available``-provenance step (option-captured, never demonstrated)
    may be dropped — a DEMONSTRATED step failing is a real signal and is
    untouchable here;
  * only ``combination``-type cases are eligible;
  * the case NAME loses the dropped ``Label=Value`` fragment (it must never
    advertise coverage it no longer has), the DESCRIPTION gains an explicit
    note naming the drop + the open question (design vs application finding),
    and a machine tag records it;
  * the scenario id is PRESERVED (run history + dossiers stay attached);
  * the ambiguity is NOT auto-closed — the recovery dossier stays open: if the
    field SHOULD have appeared for these values, that is an application
    finding a human must read.

Pure module — the router applies the returned transform inside its session.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class RegenResult:
    """The applied transform, ready to persist onto the factory row."""
    test_case: dict          # updated case JSON (steps renumbered)
    name: str                # updated, honest name
    step_count: int
    dropped_label: str       # the axis field label that was dropped
    dropped_value: str       # the value the dropped step would have set
    note: str                # human-facing description note (already appended)
    tag: str                 # machine tag (already appended to case tags)


def _norm_label(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).lower()


def drop_absent_axis(
    *, test_case: dict, name: str, test_type: str, step_number: int,
    diagnosed_at: str,
) -> RegenResult | None:
    """Drop ``step_number`` from a combination case — or refuse with ``None``.

    Refusal (None) when: not a combination case; the step doesn't exist; or the
    step is NOT ``available``-provenance (demonstrated steps are untouchable).
    """
    if str(test_type or "").strip().lower() != "combination":
        return None
    steps = list((test_case or {}).get("steps") or [])
    idx = int(step_number) - 1
    if not (0 <= idx < len(steps)) or not isinstance(steps[idx], dict):
        return None
    step = steps[idx]
    if str(step.get("provenance") or "").strip().lower() != "available":
        return None   # never drop a demonstrated (or unknown-provenance) step

    observed = dict(step.get("observed") or {})
    label = str(observed.get("label") or "").strip()
    value = str(observed.get("value") or step.get("data_ref") or "").strip()

    # ── remove + renumber ────────────────────────────────────────────────────
    new_steps = [dict(s) for i, s in enumerate(steps) if i != idx]
    for i, s in enumerate(new_steps, 1):
        s["step_number"] = i

    # ── honest NAME: the combo list must not advertise the dropped axis ──────
    new_name = name or ""
    if label:
        # matches "Label=Value" with an optional leading/trailing ", "
        frag = re.compile(
            r"(,\s*)?" + re.escape(label) + r"\s*=\s*[^,]*", re.IGNORECASE)
        candidate = frag.sub("", new_name, count=1)
        # tidy artifacts: "with , " / trailing ", " / double commas
        candidate = re.sub(r"with\s*,\s*", "with ", candidate)
        candidate = re.sub(r",\s*,", ",", candidate)
        candidate = re.sub(r",\s*$", "", candidate).strip()
        if candidate:
            new_name = candidate

    note = (
        f" NOTE: axis '{label or f'step {step_number}'}'"
        + (f"='{value}'" if value else "")
        + f" dropped by auto-recovery on {diagnosed_at} — the field was not "
        "present in the form state this combination produces (diagnosed from "
        "the live failure-state capture). Coverage is reduced accordingly. If "
        "this field SHOULD appear for these values, that is an APPLICATION "
        "finding — see the open recovery dossier."
    )
    tag = "axis-dropped:" + re.sub(r"[^a-z0-9]+", "-", _norm_label(label))[:60]

    new_case = dict(test_case)
    new_case["steps"] = new_steps
    tags = list(new_case.get("tags") or [])
    if tag not in tags:
        tags.append(tag)
    new_case["tags"] = tags
    desc = str(new_case.get("description") or "")
    if "dropped by auto-recovery" not in desc:
        new_case["description"] = (desc + note).strip()

    return RegenResult(
        test_case=new_case,
        name=new_name,
        step_count=len(new_steps),
        dropped_label=label,
        dropped_value=value,
        note=note,
        tag=tag,
    )
