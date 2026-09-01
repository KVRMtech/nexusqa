"""QE-Central S5 — the change detector (design §3.5, ``cycle/change_detector.py``).

``detect(...)`` decides WHAT changed between two crawls as the **UNION** of two
independent, fail-safe signals:

  1. the **repo-SHA diff** (repo-intel ``POST /{app_id}/diff`` — mapped atoms per
     changed file), which is *fail-safe-to-full*: if the diff is unavailable, the
     stack is unsupported, or there is no baseline SHA to diff against, we cannot
     localise the change, so the whole suite is selected;
  2. the **journey-graph fingerprint diff** (:func:`fingerprints.diff_snapshots`),
     which is *fail-safe-to-CHANGED*: an uncomputable page is CHANGED and a
     vanished page is a ``possible_deletion`` honest gap.

The result is a :class:`ChangeSet` the selector consumes.  Nothing here does
I/O — the driver fetches the live fingerprints and the repo diff, then passes
them in — so the detector is pure logic and unit-testable on synthetic inputs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import fingerprints as fp
from .fingerprints import FingerprintSnapshot, normalize_page_key

logger = logging.getLogger(__name__)

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"


# ─── Inputs ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppChangeContext:
    """The ``app`` argument to :func:`detect`.

    Assembled by the cycle driver from the ``client_apps`` row plus the app's
    baseline ``app_fingerprints`` row.  Carrying the baseline here (rather than
    re-fetching inside ``detect``) keeps the detector pure and testable.
    """

    app_id: str
    tenant_id: str
    #: The app's approved regression baseline (the last-verified fingerprints).
    #: When no baseline exists yet, pass ``FingerprintSnapshot.from_page_fingerprints({})``.
    baseline: FingerprintSnapshot
    #: Whether the app has a repo binding (drives whether a repo diff is expected).
    repo_bound: bool = False


@dataclass(frozen=True)
class RepoAtom:
    """One repo-intel app-model atom touched by a SHA diff.

    ``key`` is the atom's stable identity (goes into ``changed_atoms``); when the
    atom maps to a UI page (a route atom), ``page_key`` locates it so cases on
    that page are selected.
    """

    key: str
    kind: str = ""
    page_key: str = ""


@dataclass(frozen=True)
class RepoDiff:
    """The response of repo-intel ``POST /{app_id}/diff`` (design §3.3 / §3.5).

    ``stack_supported=False`` means repo-intel could not analyse the stack at all
    → the detector fails safe to a FULL selection (it will not trust an empty or
    partial mapping from an unsupported stack).
    """

    stack_supported: bool
    changed_files: tuple[str, ...] = ()
    mapped_atoms: tuple[RepoAtom, ...] = ()


def repo_diff_from_result(result: object) -> RepoDiff:
    """Adapt the repo-intel client's ``RepoDiffResult`` → the detector's ``RepoDiff``.

    THE honesty seam (never green-wash): the detector has NO ``fail_safe_to_full``
    field, so we collapse it here.  A ``None`` result (diff unavailable), an
    unsupported stack, OR a partial map (``fail_safe_to_full``) ALL become
    ``stack_supported=False`` → the detector runs the FULL suite.  Only a diff that
    is both supported AND fully mapped narrows.  Producer atom shape
    (``{atom_id, kind, value:{path_pattern|path}}``) → ``RepoAtom(key, kind, page_key)``.
    """
    if result is None:
        return RepoDiff(stack_supported=False)
    supported = bool(getattr(result, "stack_supported", False))
    fail_safe = bool(getattr(result, "fail_safe_to_full", True))
    if not supported or fail_safe:
        return RepoDiff(stack_supported=False)
    atoms: list[RepoAtom] = []
    for a in getattr(result, "changed_atoms", None) or ():
        if not isinstance(a, dict):
            continue
        value = a.get("value") if isinstance(a.get("value"), dict) else {}
        page_key = str(value.get("path_pattern") or value.get("path") or "")
        kind = str(a.get("kind") or "")
        key = str(a.get("atom_id") or (f"{kind}:{page_key}" if page_key else kind))
        if key:
            atoms.append(RepoAtom(key=key, kind=kind, page_key=page_key))
    changed_files = tuple(str(f) for f in (getattr(result, "changed_files", None) or ()))
    return RepoDiff(stack_supported=True, changed_files=changed_files, mapped_atoms=tuple(atoms))


# ─── Output ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HonestGaps:
    """The honest, age-able gaps a cycle must surface (never a silent green).

    Serialised into ``app_cycles.honest_gaps`` — the two design-named keys plus
    the fail-safe provenance flags that explain WHY a full/over-selection happened.
    """

    uncomputable_pages_treated_changed: tuple[str, ...] = ()
    vanished_pages_possible_deletion: tuple[str, ...] = ()
    live_graph_uncomputable: bool = False
    repo_diff_unavailable: bool = False
    repo_stack_unsupported: bool = False

    def as_dict(self) -> dict:
        return {
            "uncomputable_pages_treated_changed": list(self.uncomputable_pages_treated_changed),
            "vanished_pages_possible_deletion": list(self.vanished_pages_possible_deletion),
            "live_graph_uncomputable": self.live_graph_uncomputable,
            "repo_diff_unavailable": self.repo_diff_unavailable,
            "repo_stack_unsupported": self.repo_stack_unsupported,
        }

    @property
    def has_possible_deletion(self) -> bool:
        """True when at least one page vanished — a P0 gap the coverage verdict
        must block on (design §3.4 shrinkage guard)."""
        return bool(self.vanished_pages_possible_deletion)


@dataclass(frozen=True)
class ChangeSet:
    """What changed for one cycle — the selector's input.

    ``mode`` ∈ ``full`` | ``incremental``.  ``changed_pages`` are normalised
    page_keys; ``changed_atoms`` are control fingerprints (from the live diff)
    unioned with repo atom keys.  ``honest_gaps`` carries the age-able gaps.
    """

    mode: str
    changed_pages: frozenset[str]
    changed_atoms: frozenset[str]
    honest_gaps: HonestGaps = field(default_factory=HonestGaps)
    reason: str = ""

    @classmethod
    def full(cls, reason: str = "full crawl requested") -> "ChangeSet":
        """A FULL selection with no incremental scoping (manual/full-floor trigger
        or a driver-level fail-safe)."""
        return cls(
            mode=MODE_FULL, changed_pages=frozenset(), changed_atoms=frozenset(),
            honest_gaps=HonestGaps(), reason=reason,
        )

    @property
    def is_full(self) -> bool:
        return self.mode == MODE_FULL

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "changed_pages": sorted(self.changed_pages),
            "changed_atoms": sorted(self.changed_atoms),
            "honest_gaps": self.honest_gaps.as_dict(),
            "reason": self.reason,
        }


# ─── The detector ────────────────────────────────────────────────────────────

def detect(
    app: AppChangeContext,
    old_sha: str | None,
    new_sha: str | None,
    live_fingerprints: FingerprintSnapshot,
    repo_diff: RepoDiff | None,
) -> ChangeSet:
    """Compute the :class:`ChangeSet` for one cycle (design §3.5).

    UNION of the repo-SHA diff and the journey-graph fingerprint diff, fail-safe
    at every branch:

      * live graph uncomputable  → MODE_FULL (re-run everything);
      * repo change in scope but undiffable / stack-unsupported → MODE_FULL;
      * otherwise MODE_INCREMENTAL with the precise changed pages + atoms.

    ``old_sha``/``new_sha`` are the repo baseline and pushed commit; a repo diff
    is only "in scope" when ``new_sha`` is present (a repo-triggered cycle).
    """
    # ── 1. journey-graph fingerprint diff (fail-safe-to-CHANGED) ──
    fp_diff = fp.diff_snapshots(app.baseline, live_fingerprints)

    changed_pages: set[str] = set(fp_diff.changed_pages)
    changed_atoms: set[str] = set(fp_diff.changed_control_fps)

    # ── 2. repo-SHA diff (fail-safe-to-full) ──
    repo_in_scope = bool((new_sha or "").strip())
    repo_diff_unavailable = False
    repo_stack_unsupported = False
    repo_force_full = False

    if repo_in_scope:
        if repo_diff is None:
            # A repo change happened but we could not reach repo-intel → full.
            repo_diff_unavailable = True
            repo_force_full = True
        elif not repo_diff.stack_supported:
            # repo-intel cannot analyse this stack → do not trust any mapping.
            repo_stack_unsupported = True
            repo_force_full = True
        elif not (old_sha or "").strip():
            # No baseline SHA to diff against (e.g. first push) → cannot scope.
            repo_diff_unavailable = True
            repo_force_full = True
        else:
            for atom in repo_diff.mapped_atoms:
                key = (atom.key or "").strip()
                if key:
                    changed_atoms.add(key)
                pk = normalize_page_key(atom.page_key) if atom.page_key else ""
                if pk:
                    changed_pages.add(pk)

    # ── 3. mode + honest gaps ──
    force_full = repo_force_full or fp_diff.live_graph_uncomputable
    mode = MODE_FULL if force_full else MODE_INCREMENTAL

    gaps = HonestGaps(
        uncomputable_pages_treated_changed=tuple(sorted(fp_diff.uncomputable_pages)),
        vanished_pages_possible_deletion=tuple(sorted(fp_diff.vanished_pages)),
        live_graph_uncomputable=fp_diff.live_graph_uncomputable,
        repo_diff_unavailable=repo_diff_unavailable,
        repo_stack_unsupported=repo_stack_unsupported,
    )

    reason = _reason(mode, fp_diff, gaps, repo_in_scope, len(changed_pages), len(changed_atoms))

    logger.info(
        "qec.change_detector.detected",
        extra={
            "app_id": app.app_id, "tenant_id": app.tenant_id, "mode": mode,
            "changed_pages": len(changed_pages), "changed_atoms": len(changed_atoms),
            "vanished": len(gaps.vanished_pages_possible_deletion),
            "uncomputable": len(gaps.uncomputable_pages_treated_changed),
            "live_graph_uncomputable": gaps.live_graph_uncomputable,
            "repo_diff_unavailable": repo_diff_unavailable,
            "repo_stack_unsupported": repo_stack_unsupported,
        },
    )

    return ChangeSet(
        mode=mode,
        changed_pages=frozenset(changed_pages),
        changed_atoms=frozenset(changed_atoms),
        honest_gaps=gaps,
        reason=reason,
    )


def _reason(
    mode: str, fp_diff, gaps: HonestGaps, repo_in_scope: bool,
    n_pages: int, n_atoms: int,
) -> str:
    """A short, honest, human-readable summary of the detection outcome."""
    if mode == MODE_FULL:
        if gaps.live_graph_uncomputable:
            return "full: live journey-graph uncomputable (fail-safe re-run all)"
        if gaps.repo_stack_unsupported:
            return "full: repo stack unsupported (fail-safe re-run all)"
        if gaps.repo_diff_unavailable:
            return "full: repo change undiffable (fail-safe re-run all)"
        return "full"
    bits = [f"incremental: {n_pages} changed page(s), {n_atoms} changed atom(s)"]
    if gaps.vanished_pages_possible_deletion:
        bits.append(f"{len(gaps.vanished_pages_possible_deletion)} possible deletion(s)")
    if gaps.uncomputable_pages_treated_changed:
        bits.append(f"{len(gaps.uncomputable_pages_treated_changed)} uncomputable page(s) treated changed")
    return "; ".join(bits)


__all__ = [
    "AppChangeContext",
    "RepoAtom",
    "RepoDiff",
    "HonestGaps",
    "ChangeSet",
    "MODE_FULL",
    "MODE_INCREMENTAL",
    "detect",
]
