"""QE-Central S5 — the incremental selector (design §3.5, ``cycle/selector.py``).

Given the compiled cases, a :class:`~app.controlplane.cycle.change_detector.ChangeSet`,
and a per-case criticality band, decide which cases to RUN this cycle and which
to CARRY FORWARD with an age-labelled verdict.

Selection rule (design §3.5) — a case RUNS iff ANY of:
  * ``mode == full``                       (a full-floor / manual-full cycle);
  * its band is **P0**                     (the criticality floor: P0 ALWAYS runs);
  * any of its step page_keys ∈ changed    (a changed — or vanished — page);
  * any of its control_fps ∈ changed_atoms (a changed navigating control);
  * it has **no prior green verdict** to carry;
  * its carried verdict has reached the **max-carry TTL** (staleness bound).

Otherwise the case is CARRIED FORWARD: its prior verdict_run_id is preserved and
its ``verdict_age_cycles`` is incremented — an explicit, time-bounded, age-labelled
verdict.  A carried case is NEVER a silent green: it always reports how stale its
last real run is, and it is force-re-run once the TTL is reached.

The impact analysis reuses the ``affected_scenarios`` PATTERN (diff.py:441-521),
reimplemented over the case→(page_key, control_fp) mapping: per case we collect
exactly which changed pages/controls it cites, and name them in the reason.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .change_detector import ChangeSet
from .fingerprints import normalize_page_key

logger = logging.getLogger(__name__)

BAND_P0 = "P0"
_DEFAULT_BAND = "P2"

# The max-carry TTL is a founder-approved honesty parameter (design open
# decision #4) — env-driven, read at call time so it is overridable per
# deployment and in tests without import-time capture.
_DEFAULT_CARRY_TTL_CYCLES = 5


def default_carry_ttl_cycles() -> int:
    """The max number of cycles a green verdict may be carried before a forced
    re-run.  ``QEC_CARRY_TTL_CYCLES`` (>= 0) overrides the default; a missing or
    malformed value falls back to :data:`_DEFAULT_CARRY_TTL_CYCLES`."""
    raw = os.environ.get("QEC_CARRY_TTL_CYCLES", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_CARRY_TTL_CYCLES
    return v if v >= 0 else _DEFAULT_CARRY_TTL_CYCLES


# ─── Inputs / outputs ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaseRef:
    """One compiled case's selection-relevant projection.

    ``page_keys``/``control_fps`` are the pages and navigating controls the case's
    steps touch (the driver derives them from the compiled steps).  The verdict
    fields carry the case's last real run so an unchanged case can be carried
    forward honestly:
      * ``last_verdict_run_id`` — the run that last proved it green ("" ⇒ none);
      * ``verdict_age_cycles``  — cycles the current verdict has already been
        carried without a re-run (0 ⇒ freshly run last cycle).
    """

    test_id: str
    page_keys: tuple[str, ...] = ()
    control_fps: tuple[str, ...] = ()
    criticality: str = _DEFAULT_BAND
    last_verdict_run_id: str = ""
    verdict_age_cycles: int = 0

    @classmethod
    def from_obj(cls, obj: "CaseRef | Mapping") -> "CaseRef":
        """Coerce a mapping (e.g. a row the driver loaded) into a CaseRef."""
        if isinstance(obj, CaseRef):
            return obj
        if not isinstance(obj, Mapping):
            raise TypeError(f"cannot coerce {type(obj).__name__} to CaseRef")
        return cls(
            test_id=str(obj.get("test_id") or ""),
            page_keys=tuple(str(p) for p in (obj.get("page_keys") or ()) if p),
            control_fps=tuple(str(f) for f in (obj.get("control_fps") or ()) if f),
            criticality=str(obj.get("criticality") or _DEFAULT_BAND),
            last_verdict_run_id=str(obj.get("last_verdict_run_id") or ""),
            verdict_age_cycles=int(obj.get("verdict_age_cycles") or 0),
        )


@dataclass(frozen=True)
class CarriedForward:
    """A case NOT run this cycle — its verdict carried forward, age-labelled."""

    test_id: str
    verdict_run_id: str
    verdict_age_cycles: int   # the NEW (incremented) age
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "verdict_run_id": self.verdict_run_id,
            "verdict_age_cycles": self.verdict_age_cycles,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SelectionResult:
    """The selection outcome — serialises to ``app_cycles.selected_scope``."""

    mode: str
    selected_test_ids: tuple[str, ...]
    carried_forward: tuple[CarriedForward, ...]
    selection_reason: str
    per_case_reasons: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "selected_test_ids": list(self.selected_test_ids),
            "carried_forward": [c.as_dict() for c in self.carried_forward],
            "selection_reason": self.selection_reason,
            "per_case_reasons": dict(self.per_case_reasons),
        }


# ─── Selection ───────────────────────────────────────────────────────────────

def _band_for(case: CaseRef, criticality: Mapping[str, str]) -> str:
    """Resolve a case's band: the explicit map wins, else the case's own band,
    else the default.  Normalised upper-case so "p0" == "P0"."""
    band = (criticality.get(case.test_id) if criticality else None) or case.criticality or _DEFAULT_BAND
    return str(band).strip().upper()


def _hit_reasons(hit_pages: list[str], hit_fps: list[str]) -> list[str]:
    """Name the exact changed pages/controls a case cites (affected_scenarios
    PATTERN — diff.py:441-521, reimplemented over the case→page/control map)."""
    reasons: list[str] = []
    for pk in hit_pages:
        reasons.append(f"step page {pk!r} changed")
    for fp in hit_fps:
        reasons.append(f"control {fp[:12]}… changed")
    return reasons


def select(
    cases: Iterable["CaseRef | Mapping"],
    change_set: ChangeSet,
    criticality: Mapping[str, str] | None = None,
    *,
    carry_ttl_cycles: int | None = None,
) -> SelectionResult:
    """Select the cases to run this cycle; carry the rest forward (age-labelled).

    ``criticality`` maps ``test_id -> band`` (P0/P1/P2/P3); a case absent from it
    falls back to its own ``criticality`` field.  ``carry_ttl_cycles`` bounds how
    stale a carried verdict may get before a forced re-run (defaults to
    :func:`default_carry_ttl_cycles`).
    """
    criticality = criticality or {}
    ttl = carry_ttl_cycles if carry_ttl_cycles is not None else default_carry_ttl_cycles()
    changed_pages = change_set.changed_pages
    changed_atoms = change_set.changed_atoms
    is_full = change_set.is_full

    selected: list[str] = []
    carried: list[CarriedForward] = []
    per_case_reasons: dict[str, str] = {}
    counts = {"full": 0, "p0_floor": 0, "changed": 0, "no_verdict": 0, "ttl": 0}

    for raw in cases:
        case = CaseRef.from_obj(raw)
        test_id = case.test_id
        band = _band_for(case, criticality)

        norm_pages = {normalize_page_key(pk) for pk in case.page_keys if pk}
        hit_pages = sorted(norm_pages & changed_pages)
        hit_fps = sorted(set(case.control_fps) & changed_atoms)

        if is_full:
            counts["full"] += 1
            per_case_reasons[test_id] = f"mode=full: {change_set.reason or 'full crawl'}"
            selected.append(test_id)
            continue

        if band == BAND_P0:
            counts["p0_floor"] += 1
            reasons = ["P0 criticality floor: always runs"]
            reasons += _hit_reasons(hit_pages, hit_fps)  # transparency, not required
            per_case_reasons[test_id] = "; ".join(reasons)
            selected.append(test_id)
            continue

        if hit_pages or hit_fps:
            counts["changed"] += 1
            per_case_reasons[test_id] = "; ".join(_hit_reasons(hit_pages, hit_fps))
            selected.append(test_id)
            continue

        if not case.last_verdict_run_id:
            counts["no_verdict"] += 1
            per_case_reasons[test_id] = "no prior green verdict to carry — must run"
            selected.append(test_id)
            continue

        if case.verdict_age_cycles >= ttl:
            counts["ttl"] += 1
            per_case_reasons[test_id] = (
                f"carry TTL reached (age {case.verdict_age_cycles} >= {ttl}) — "
                f"re-run for a fresh verdict"
            )
            selected.append(test_id)
            continue

        # Unchanged, has a verdict, within TTL → carry forward with age+1.
        new_age = case.verdict_age_cycles + 1
        reason = (
            f"unchanged; carried forward (verdict {case.last_verdict_run_id}, "
            f"age {new_age}/{ttl} cycles)"
        )
        carried.append(CarriedForward(
            test_id=test_id, verdict_run_id=case.last_verdict_run_id,
            verdict_age_cycles=new_age, reason=reason,
        ))
        per_case_reasons[test_id] = reason

    summary = _summary(is_full, change_set, len(selected), len(carried), counts, ttl)

    logger.info(
        "qec.selector.selected",
        extra={"mode": change_set.mode, "selected": len(selected),
               "carried": len(carried), **counts},
    )

    return SelectionResult(
        mode=change_set.mode,
        selected_test_ids=tuple(selected),
        carried_forward=tuple(carried),
        selection_reason=summary,
        per_case_reasons=per_case_reasons,
    )


def _summary(
    is_full: bool, change_set: ChangeSet, n_selected: int, n_carried: int,
    counts: Mapping[str, int], ttl: int,
) -> str:
    """A one-line honest summary for ``selected_scope.selection_reason``."""
    if is_full:
        return f"full: selected all {n_selected} case(s) ({change_set.reason or 'full crawl'})"
    parts = []
    if counts["changed"]:
        parts.append(f"{counts['changed']} changed")
    if counts["p0_floor"]:
        parts.append(f"{counts['p0_floor']} P0-floor")
    if counts["no_verdict"]:
        parts.append(f"{counts['no_verdict']} no-verdict")
    if counts["ttl"]:
        parts.append(f"{counts['ttl']} TTL({ttl})-expired")
    breakdown = ", ".join(parts) if parts else "none"
    gaps = change_set.honest_gaps
    tail = ""
    if gaps.vanished_pages_possible_deletion:
        tail = f"; {len(gaps.vanished_pages_possible_deletion)} possible-deletion page(s) forced re-run"
    return (
        f"incremental: selected {n_selected} ({breakdown}); "
        f"carried {n_carried} with age-labelled verdicts{tail}"
    )


__all__ = [
    "CaseRef",
    "CarriedForward",
    "SelectionResult",
    "BAND_P0",
    "default_carry_ttl_cycles",
    "select",
]
