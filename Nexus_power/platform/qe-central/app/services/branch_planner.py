"""Branch planner — turns the graph's unwalked branches into walk plans.

E1: a plan forces exactly ONE not-yet-walked option and leaves everything
else at its default — single-variable enumeration.  Every discovered option
becomes its own plan; the autowalk loop drives until the branch ledger has
no ``discovered`` option left (all are walked, blocked, or deferred).

Explosion control: when a journey's discovered backlog exceeds the
``journey_path_enum_cap``, excess branches are ``deferred`` with an honest
count — never silently truncated, never claimed as "all combinations".

Priorities: journeys whose paths display outcomes first (a different premium
IS a different business path — the highest-value proof), then the largest
discovered backlog.

Source traversal: a COMPLETED traversal is preferred, falling back to one
that ended decision-blocked (see ``_DECISION_BLOCKED_TERMINALS``).  Planning
only off completed traversals deadlocks a journey stopped at an unmade
business decision — it cannot complete until an option is forced, and no
option is forced until it completes.

Status law: ``discovered → planned`` at dispatch; the completion fold
upgrades ``planned → walked`` when the option was genuinely taken; the
reconciler here marks what a planned walk did NOT reach as ``blocked`` with
its attributed reason — surfaced, never silently retried. ``walked`` never
downgrades.  ``deferred`` is honest: "option exists, exceeds cap."

This module is PURE PLANNING + STATUS: it never dispatches. Dispatch
orchestration (flags, caps, the actual crawl) lives at the router layer.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import or_ as sa_or
from sqlalchemy import select

from ..config import settings
from ..db import tenant_scoped_qec_session, utc_now
from ..db.fleet_models import TenantProvisioningRow
from ..db.journey_models import (
    JourneyBranchRow,
    JourneyNodeRow,
    JourneyRow,
    JourneyTraversalRow,
)
from .journey_fold import (
    BRANCH_BLOCKED,
    BRANCH_EQUIVALENT,
    BRANCH_DEFERRED,
    BRANCH_DISCOVERED,
    BRANCH_PLANNED,
    BRANCH_WALKED,
)

logger = logging.getLogger(__name__)

#: Traversal terminals that mean "the funnel REFUSED to advance", as opposed
#: to "the run broke or was cut short".  A journey that stops at an unmade
#: business decision (which insurance type? which coverage tier?) can only
#: ever end this way: the crawl is forbidden from choosing an option itself,
#: so it clicks Continue, the page validates and stays put, and the loop
#: detector ends the walk — ``completed`` is False and always will be.
#:
#: Planning branches ONLY off completed traversals therefore deadlocks the
#: exact case branch walking exists to break: the journey cannot complete
#: until an option is forced, and no option is forced until the journey
#: completes.  These two terminals are the sanctioned way out of that circle.
#:
#: Deliberately EXCLUDED: ``budget_exhausted`` / ``cancelled`` (the walk was
#: cut short — its path is a fragment, not a finding) and
#: ``oracle_unavailable`` (whether it advances is honestly UNKNOWN; planning
#: off a guess would launder that unknown into a claim).
_DECISION_BLOCKED_TERMINALS = frozenset({"loop", "no_advance"})


async def autonomy_flags(tenant_id: str) -> dict[str, bool]:
    """The tenant's autonomy posture, FAIL-CLOSED: each capability needs BOTH
    its env-level switch AND the tenant's explicit flag."""
    tenant_branch = tenant_autowalk = False
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            row = (await session.execute(
                select(TenantProvisioningRow.branch_walks_enabled,
                       TenantProvisioningRow.journey_autowalk).where(
                    TenantProvisioningRow.tenant_id == tenant_id,
                ))).one_or_none()
            if row is not None:
                tenant_branch, tenant_autowalk = bool(row[0]), bool(row[1])
    except Exception as exc:
        logger.warning("qec.branch_planner.flags_read_failed",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:200]})
    return {
        "branch_walks": settings.branch_walks_enabled and tenant_branch,
        "autowalk": (settings.journey_autowalk_enabled and tenant_autowalk
                     and settings.branch_walks_enabled and tenant_branch),
    }


def _identity_ref(overrides: dict[str, str]) -> str:
    """The constrained-identity marker stamped onto the traversal: the crawl's
    stable synthetic identity, constrained by exactly these planned choices —
    auditable, value-free."""
    basis = "\x1f".join(f"{k}={v}" for k, v in sorted(overrides.items()))
    return "synthetic+planned:" + hashlib.sha256(
        basis.encode("utf-8")).hexdigest()[:12]


async def plan_walks(
    *, tenant_id: str, app_id: str, journey_id: Optional[str] = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Walk plans for this app's ``discovered`` branch backlog.

    E1: one plan per discovered option — single-variable enumeration.  Each
    plan forces exactly one option; everything else takes its default.  Plans
    are capped per-cycle by ``branch_walks_per_cycle``; excess discovered
    branches beyond ``journey_path_enum_cap`` per journey are deferred with
    an honest count.

    Returns ``[{journey_id, business_name, branch_ids, choice_overrides,
    identity_ref}]``, highest-value journeys first, then ordered by step
    proximity to the entry."""
    cap = limit or settings.branch_walks_per_cycle
    enum_cap = settings.journey_path_enum_cap
    plans: list[dict[str, Any]] = []
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey_q = select(JourneyRow).where(
            JourneyRow.tenant_id == tenant_id, JourneyRow.app_id == app_id)
        if journey_id:
            journey_q = journey_q.where(JourneyRow.journey_id == journey_id)
        journeys = (await session.execute(journey_q)).scalars().all()

        scored: list[tuple[int, int, JourneyRow, list[str]]] = []
        for j in journeys:
            # A completed traversal is always preferred; a decision-blocked one
            # is the fallback that breaks the deadlock described on
            # _DECISION_BLOCKED_TERMINALS.  Ordering by ``completed`` first
            # keeps the old behaviour byte-for-byte whenever a completed
            # traversal exists, so this only ever ADDS reachable plans.
            traversal = (await session.execute(
                select(JourneyTraversalRow).where(
                    JourneyTraversalRow.tenant_id == tenant_id,
                    JourneyTraversalRow.app_id == app_id,
                    JourneyTraversalRow.journey_id == j.journey_id,
                    sa_or(
                        JourneyTraversalRow.completed.is_(True),
                        JourneyTraversalRow.terminal.in_(
                            sorted(_DECISION_BLOCKED_TERMINALS)),
                    ),
                ).order_by(JourneyTraversalRow.completed.desc(),
                           JourneyTraversalRow.created_at.desc())
                .limit(1))).scalar_one_or_none()
            if traversal is None:
                continue
            path_fps = [str(fp) for fp in (traversal.path_fps or [])]
            if not path_fps:
                continue
            discovered = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                    JourneyBranchRow.node_fp.in_(path_fps),
                    JourneyBranchRow.status == BRANCH_DISCOVERED,
                ))).scalars().all()
            if not discovered:
                continue
            has_outcome = (await session.execute(
                select(JourneyNodeRow.node_id).where(
                    JourneyNodeRow.tenant_id == tenant_id,
                    JourneyNodeRow.app_id == app_id,
                    JourneyNodeRow.fingerprint.in_(path_fps),
                    JourneyNodeRow.has_outcome.is_(True),
                ).limit(1))).scalar_one_or_none() is not None
            scored.append((1 if has_outcome else 0, len(discovered), j,
                           path_fps))

        scored.sort(key=lambda t: (-t[0], -t[1]))
        for _, _, journey, path_fps in scored:
            if len(plans) >= cap:
                break
            discovered = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                    JourneyBranchRow.node_fp.in_(path_fps),
                    JourneyBranchRow.status == BRANCH_DISCOVERED,
                ))).scalars().all()
            order = {fp: i for i, fp in enumerate(path_fps)}
            discovered.sort(key=lambda b: (order.get(b.node_fp, 1 << 30),
                                           b.control_signature,
                                           b.option_label_norm))
            # E1 explosion control: defer excess beyond the cap with an
            # honest count — never silently truncated.
            if len(discovered) > enum_cap:
                to_defer = discovered[enum_cap:]
                discovered = discovered[:enum_cap]
                for b in to_defer:
                    if b.status == BRANCH_DISCOVERED:
                        b.status = BRANCH_DEFERRED
                        b.blocked_reason = (
                            f"deferred: {len(to_defer)} options exceed the "
                            f"per-journey enumeration cap ({enum_cap})")[:400]
                        b.last_status_at = utc_now()
                logger.warning(
                    "qec.branch_planner.deferred tenant=%s journey=%s "
                    "deferred=%d cap=%d",
                    tenant_id, journey.journey_id, len(to_defer), enum_cap)

            # PROBE BUDGET. Until a decision has been shown to fork the business,
            # plan only a couple of its options. A 23-option state picker and a
            # 13-option height picker are data variations, and queueing every
            # value before any evidence exists is what made the sweep unbounded.
            # Once the representatives come back DIFFERENT, the cap lifts and the
            # decision is enumerated properly.
            probe_k = max(2, int(settings.branch_probe_k))
            planned_per_decision: dict[str, int] = {}
            for b in discovered:
                already = sum(
                    1 for x in (await session.execute(
                        select(JourneyBranchRow.status).where(
                            JourneyBranchRow.tenant_id == tenant_id,
                            JourneyBranchRow.app_id == app_id,
                            JourneyBranchRow.control_signature == b.control_signature,
                        ))).scalars().all()
                    if x in (BRANCH_WALKED, BRANCH_PLANNED))
                if already + planned_per_decision.get(b.control_signature, 0) >= probe_k:
                    continue          # enough representatives in flight already
                if len(plans) >= cap:
                    break
                planned_per_decision[b.control_signature] = (
                    planned_per_decision.get(b.control_signature, 0) + 1)
                overrides = {b.control_signature: b.option_label_norm}
                plans.append({
                    "journey_id": journey.journey_id,
                    "business_name": journey.business_name,
                    "branch_ids": [b.branch_id],
                    "choice_overrides": overrides,
                    "identity_ref": _identity_ref(overrides),
                })
    return plans


async def plan_pairwise_walks(
    *, tenant_id: str, app_id: str, journey_id: str,
    must_walk: list[dict[str, str]] | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """E2 — pairwise combination plans for a journey's decision controls.

    Groups the journey's branches by ``(control_signature)`` into factors,
    each branch option is a level.  Generates a pairwise covering array
    (minimum configurations covering every option-pair across any two
    controls), seeds with must-walk client scenarios, and filters out
    combinations already walked.

    Returns plans with multi-choice ``choice_overrides`` — each plan
    forces one specific option per decision control in the combination.
    """
    from .pairwise import Factor, factors_from_branches, generate_pairwise

    cap = limit or settings.pairwise_walks_per_cycle
    plans: list[dict[str, Any]] = []
    async with tenant_scoped_qec_session(tenant_id) as session:
        journey = (await session.execute(
            select(JourneyRow).where(
                JourneyRow.tenant_id == tenant_id,
                JourneyRow.app_id == app_id,
                JourneyRow.journey_id == journey_id,
            ))).scalar_one_or_none()
        if journey is None:
            return []

        traversal = (await session.execute(
            select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant_id,
                JourneyTraversalRow.app_id == app_id,
                JourneyTraversalRow.journey_id == journey_id,
                JourneyTraversalRow.completed.is_(True),
            ).order_by(JourneyTraversalRow.created_at.desc())
            .limit(1))).scalar_one_or_none()
        if traversal is None:
            return []

        path_fps = [str(fp) for fp in (traversal.path_fps or [])]
        if not path_fps:
            return []

        all_branches = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.app_id == app_id,
                JourneyBranchRow.node_fp.in_(path_fps),
                JourneyBranchRow.status.in_([
                    BRANCH_WALKED, BRANCH_DISCOVERED,
                    BRANCH_PLANNED, BRANCH_BLOCKED,
                ]),
            ))).scalars().all()
        if not all_branches:
            return []

        branch_dicts = [
            {"control_signature": b.control_signature,
             "option_label_norm": b.option_label_norm,
             "status": b.status,
             "branch_id": b.branch_id}
            for b in all_branches
        ]
        factors = factors_from_branches(branch_dicts)
        if len(factors) < 2:
            return []

        walked_combos: set[str] = set()
        walked_traversals = (await session.execute(
            select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant_id,
                JourneyTraversalRow.app_id == app_id,
                JourneyTraversalRow.journey_id == journey_id,
                JourneyTraversalRow.completed.is_(True),
            ))).scalars().all()
        for t in walked_traversals:
            ref = t.identity_ref or ""
            if ref:
                walked_combos.add(ref)

        result = generate_pairwise(
            factors, must_walk=must_walk, max_configs=cap * 3)

        branch_lookup: dict[tuple[str, str], str] = {}
        for b in all_branches:
            branch_lookup[(b.control_signature, b.option_label_norm)] = b.branch_id

        for config in result.configurations:
            if len(plans) >= cap:
                break
            overrides = dict(config)
            ref = _identity_ref(overrides)
            if ref in walked_combos:
                continue
            branch_ids = [
                branch_lookup[(sig, opt)]
                for sig, opt in overrides.items()
                if (sig, opt) in branch_lookup
            ]
            plans.append({
                "journey_id": journey_id,
                "business_name": journey.business_name,
                "branch_ids": branch_ids,
                "choice_overrides": overrides,
                "identity_ref": ref,
                "pairwise": True,
            })
            walked_combos.add(ref)

    logger.warning(
        "qec.branch_planner.pairwise tenant=%s app=%s journey=%s "
        "factors=%d total_pairs=%d covered=%d plans=%d must_walk=%d",
        tenant_id, app_id, journey_id, len(factors),
        result.total_pairs, result.covered_pairs,
        len(plans), result.must_walk_count)
    return plans


async def mark_planned(*, tenant_id: str, branch_ids: list[str]) -> int:
    """``discovered → planned`` at dispatch (walked/blocked are untouched)."""
    if not branch_ids:
        return 0
    changed = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.branch_id.in_(branch_ids),
                JourneyBranchRow.status == BRANCH_DISCOVERED,
            ))).scalars().all()
        for b in rows:
            b.status = BRANCH_PLANNED
            b.last_status_at = utc_now()
            changed += 1
    return changed


def _outcome_shape(outcome_values: Any) -> tuple:
    """What a journey PRODUCED, compared by shape and value.

    A different premium IS a different business path — that is the whole test
    for whether a decision forks the business or merely varies its data.
    """
    if not isinstance(outcome_values, (list, tuple)):
        return ()
    out = []
    for v in outcome_values:
        if not isinstance(v, Mapping):
            continue
        out.append((str(v.get("label") or "").strip().lower(),
                    str(v.get("value") or "").strip().lower(),
                    str(v.get("value_type") or "").strip().lower()))
    return tuple(sorted(out))


async def classify_equivalent_options(*, tenant_id: str, app_id: str) -> dict[str, int]:
    """Retire the options of a decision that is a DATA VARIATION, not a fork.

    For each decision with at least ``branch_probe_k`` walked options: if every
    walked representative produced the SAME path AND the SAME outcome, the
    decision does not fork the business — the remaining options are marked
    ``equivalent`` with an honest count instead of being enumerated.

    This is measured, never guessed. Options are retired only AFTER real walks
    disagreed about nothing; a decision whose representatives produced different
    paths or different premiums keeps every one of its options walkable.

    Without it "exhaustive" means the cartesian product of every dropdown on
    every page: 23 US states and 13 height-in-inches each became their own crawl,
    the sweep never converged, and one application held a fleet-wide lock for
    hours. Best-effort — a failure here only means less pruning, never a wrong
    coverage claim."""
    probe_k = max(2, int(settings.branch_probe_k))
    retired = decisions = 0
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            rows = (await session.execute(
                select(JourneyBranchRow).where(
                    JourneyBranchRow.tenant_id == tenant_id,
                    JourneyBranchRow.app_id == app_id,
                ))).scalars().all()
            by_decision: dict[str, list[JourneyBranchRow]] = {}
            for b in rows:
                by_decision.setdefault(b.control_signature, []).append(b)

            for sig, branches in by_decision.items():
                walked = [b for b in branches if b.status == BRANCH_WALKED
                          and b.walked_in_traversal]
                pending = [b for b in branches if b.status == BRANCH_DISCOVERED]
                if len(walked) < probe_k or not pending:
                    continue
                trav_ids = sorted({b.walked_in_traversal for b in walked})
                travs = (await session.execute(
                    select(JourneyTraversalRow).where(
                        JourneyTraversalRow.tenant_id == tenant_id,
                        JourneyTraversalRow.app_id == app_id,
                        JourneyTraversalRow.traversal_id.in_(trav_ids),
                    ))).scalars().all()
                if len(travs) < probe_k:
                    continue          # cannot compare what we cannot read
                fingerprints = {
                    (t.path_hash, _outcome_shape(t.outcome_values)) for t in travs}
                if len(fingerprints) != 1:
                    continue          # the options genuinely fork — keep walking
                note = (f"equivalent: {len(walked)} option(s) of this decision were "
                        f"walked and produced the same path and the same outcome, "
                        f"so this option exercises no new business behaviour")[:400]
                for b in pending:
                    b.status = BRANCH_EQUIVALENT
                    b.blocked_reason = note
                    b.last_status_at = utc_now()
                    retired += 1
                decisions += 1
    except Exception as exc:
        logger.warning("qec.branch_planner.equivalence_failed",
                       extra={"tenant_id": tenant_id, "app_id": app_id,
                              "error": str(exc)[:200]})
        return {"decisions": 0, "retired": 0}
    if retired:
        logger.warning(
            "qec.branch_planner.equivalent decisions=%d retired=%d tenant=%s app=%s",
            decisions, retired, tenant_id, app_id)
    return {"decisions": decisions, "retired": retired}


async def unmark_planned(*, tenant_id: str, branch_ids: list[str]) -> int:
    """``planned → discovered`` — the dispatch never happened, so put the option
    back on the backlog untouched.

    This is NOT the same as ``blocked``.  ``blocked`` is a finding ("this option
    was planned, the walk ran, and it did not reach it") and it is terminal.  A
    dispatch that was REFUSED — the single-flight explorer answering 409 busy —
    produced no walk and therefore no finding; recording it as blocked retires an
    option nobody ever tried.  Observed live: a 4-plan cycle against a
    single-worker pool burned 3 options per round on back-pressure alone.

    ``walked`` and ``blocked`` are never downgraded."""
    if not branch_ids:
        return 0
    changed = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.branch_id.in_(branch_ids),
                JourneyBranchRow.status == BRANCH_PLANNED,
            ))).scalars().all()
        for b in rows:
            b.status = BRANCH_DISCOVERED
            b.last_status_at = utc_now()
            changed += 1
    return changed


async def reconcile_completion(
    *, tenant_id: str, app_id: str, walk_plan: dict[str, Any],
    terminal_reason: str,
) -> dict[str, int]:
    """After a planned walk's fold: what the walk did NOT upgrade to
    ``walked`` becomes ``blocked`` with its attributed reason — a first-class
    surfaced fact, never a silent retry loop."""
    branch_ids = [str(b) for b in (walk_plan or {}).get("branch_ids") or []]
    if not branch_ids:
        return {"walked": 0, "blocked": 0}
    walked = blocked = 0
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.app_id == app_id,
                JourneyBranchRow.branch_id.in_(branch_ids),
            ))).scalars().all()
        for b in rows:
            if b.status == BRANCH_WALKED:
                walked += 1
                continue
            if b.status == BRANCH_PLANNED:
                b.status = BRANCH_BLOCKED
                b.blocked_reason = (
                    "planned walk completed without reaching this option "
                    f"(walk terminal: {str(terminal_reason or 'unknown')[:60]})"
                )[:400]
                b.last_status_at = utc_now()
                blocked += 1
    logger.warning(
        "qec.branch_planner.reconciled tenant=%s app=%s walked=%d blocked=%d",
        tenant_id, app_id, walked, blocked)
    return {"walked": walked, "blocked": blocked}
