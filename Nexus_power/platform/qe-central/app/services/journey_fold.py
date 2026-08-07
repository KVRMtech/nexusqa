"""Journey fold — a finished crawl's flows become graph rows.

The fold is the seam where the flow ledger's honest paths turn into the
Journey Graph (Release C1): nodes upsert by fingerprint, edges by
(from, to, trigger), each walked flow becomes ONE traversal, and every
enumerated option at a decision point becomes a branch row — walked or NOT.

Laws:
  * IDEMPOTENT — every id is derived from its natural key, traversals dedup
    on (exploration, journey, path hash), and count bumps happen only when a
    traversal is NEW. Re-folding the same crawl is a no-op.
  * ``completed`` is copied from the ledger flow (derived at source) — the
    fold can never upgrade a truncated walk.
  * ``walked`` never downgrades; ``discovered`` never overwrites a walked /
    planned / blocked branch.
  * BEST-EFFORT at the call seam — a fold failure logs and never breaks a
    completion callback; the manifest stays the source of truth and any crawl
    can be re-folded later (the replay endpoint).
  * Value-free: labels, kinds, signatures, titles. No user values enter the
    graph.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import (
    JourneyBranchRow,
    JourneyEdgeRow,
    JourneyNodeRow,
    JourneyRow,
    JourneyTraversalRow,
)
from .advance_memory import normalize_label
from .catalog import (
    build_ledger_by_url, build_states_index,
    extract_controls, extract_outcomes,
    merge_controls, merge_outcomes,
)
from .journey_baseline import BASELINE_CAPTURED, detect_drift

logger = logging.getLogger(__name__)

#: Branch statuses (journey_branches.status).
BRANCH_WALKED = "walked"
BRANCH_DISCOVERED = "discovered"
BRANCH_PLANNED = "planned"
BRANCH_BLOCKED = "blocked"
#: E1: the branch exceeds the per-journey explosion cap. Honest, with a count
#: of what was deferred.  Never silently truncated.
BRANCH_DEFERRED = "deferred"
#: This option belongs to a DATA VARIATION, not a business fork: other options of
#: the same decision were walked and produced the SAME path and the SAME outcome,
#: so walking this one would prove nothing new.
#:
#: "Term Life vs Whole Life" is a business path — different product, different
#: premium logic. "Alabama vs Alaska" is the same journey with different data.
#: Enumerating the second kind is what turned one 5-page form into 113 branches
#: (23 US states, 13 height-in-inches), each its own crawl, holding a global
#: single-flight lock for hours — and then reporting 113 "proven business paths"
#: where there are about six.
#:
#: EARNED, never assumed: an option is only equivalent once representatives have
#: actually been walked and compared. The count is surfaced so a reader sees
#: "1 of 23 walked, 22 equivalent" rather than a silent truncation.
BRANCH_EQUIVALENT = "equivalent"

#: Terminals that mean the traversal covered its journey — mirrored from the
#: explorer's ``flow_ledger.COMPLETING_TERMINALS`` (services share no library;
#: the fold still COPIES the flow's own ``completed`` and uses this only as a
#: defensive fallback for malformed rows).
_COMPLETING_TERMINALS = frozenset({"submit_boundary", "no_advance"})


def _sid(*parts: str) -> str:
    """Deterministic row id from a natural key — the upsert identity."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


def flows_of(coverage: Any) -> list[Mapping[str, Any]]:
    """The ledger flows a completion's coverage carries (tolerant)."""
    if not isinstance(coverage, Mapping):
        return []
    return [f for f in (coverage.get("flows") or []) if isinstance(f, Mapping)]


def is_pre_hardening(coverage: Any) -> bool:
    """A manifest folded from a crawler that predates the Release-A/B gates —
    detected by the absence of the P3 tier rollup in its flow summary."""
    if not isinstance(coverage, Mapping):
        return True
    summary = coverage.get("flow_summary")
    return not (isinstance(summary, Mapping) and "advances_by_tier" in summary)


def path_hash_of(steps: Sequence[Mapping[str, Any]]) -> str:
    fps = [str(s.get("fingerprint") or "") for s in steps if isinstance(s, Mapping)]
    return hashlib.sha256("\x1f".join(fps).encode("utf-8")).hexdigest()[:64]


async def fold_crawl(
    *, tenant_id: str, app_id: str, exploration_id: str, coverage: Any,
    identity_ref: str = "", env_ref: str = "",
) -> dict[str, int]:
    """Fold one crawl's flows into the tenant's journey graph.

    Returns the fold report (counts). Raises nothing to the caller path that
    wires it into the completion callback — the wrapper there catches; this
    function itself raises only on DB-session failure so the replay endpoint
    can surface a real error.
    """
    flows = flows_of(coverage)
    report = {"flows": len(flows), "journeys": 0, "nodes": 0, "edges": 0,
              "traversals": 0, "branches": 0, "drift_checks": 0}
    if not flows:
        return report
    pre_hardening = is_pre_hardening(coverage)
    now = utc_now()
    drift_candidates: list[tuple[str, str, list]] = []

    states_index = build_states_index(coverage)
    ledger_by_url = build_ledger_by_url(coverage)

    async with tenant_scoped_qec_session(tenant_id) as session:
        for flow in flows:
            steps = [s for s in (flow.get("steps") or []) if isinstance(s, Mapping)]
            if not steps:
                continue
            entry_fp = str(flow.get("entry_fingerprint")
                           or steps[0].get("fingerprint") or "")
            if not entry_fp:
                continue
            terminal = str(flow.get("terminal") or "")
            completed = bool(flow.get("completed",
                                      terminal in _COMPLETING_TERMINALS))

            # ── Journey (entry-keyed; ledger flow_id preserved) ───────────
            journey_id = _sid("journey", tenant_id, app_id, entry_fp)
            journey = (await session.execute(
                select(JourneyRow).where(
                    JourneyRow.tenant_id == tenant_id,
                    JourneyRow.app_id == app_id,
                    JourneyRow.entry_fingerprint == entry_fp,
                ))).scalar_one_or_none()
            if journey is None:
                journey = JourneyRow(
                    journey_id=journey_id, tenant_id=tenant_id, app_id=app_id,
                    entry_fingerprint=entry_fp,
                    flow_id=str(flow.get("flow_id") or ""),
                    entry_url=str(flow.get("entry_url") or "")[:2000],
                    entry_title=str(flow.get("entry_title") or "")[:200],
                    business_name=str(flow.get("entry_title") or "")[:200],
                    name_source="fallback",
                )
                session.add(journey)
                report["journeys"] += 1
            journey.deepest_steps = max(journey.deepest_steps or 0, len(steps))
            if completed:
                journey.last_proven_at = now

            # ── Traversal (dedup = idempotency anchor) ────────────────────
            p_hash = path_hash_of(steps)
            traversal_id = _sid("traversal", tenant_id, app_id,
                                exploration_id, journey.journey_id, p_hash)
            existing_traversal = (await session.execute(
                select(JourneyTraversalRow.traversal_id).where(
                    JourneyTraversalRow.tenant_id == tenant_id,
                    JourneyTraversalRow.app_id == app_id,
                    JourneyTraversalRow.exploration_id == exploration_id,
                    JourneyTraversalRow.journey_id == journey.journey_id,
                    JourneyTraversalRow.path_hash == p_hash,
                ))).scalar_one_or_none()
            is_new_traversal = existing_traversal is None
            if is_new_traversal:
                session.add(JourneyTraversalRow(
                    traversal_id=traversal_id, tenant_id=tenant_id,
                    app_id=app_id, journey_id=journey.journey_id,
                    exploration_id=exploration_id, terminal=terminal,
                    completed=completed,
                    fully_answered=bool(flow.get("fully_answered")),
                    path_fps=[str(s.get("fingerprint") or "") for s in steps],
                    path_hash=p_hash,
                    identity_ref=identity_ref[:200], env_ref=env_ref[:200],
                    outcome_values=[
                        {"label": str(v.get("label") or "")[:120],
                         "value": str(v.get("value") or "")[:200],
                         "value_type": str(v.get("value_type") or "")[:40]}
                        for v in (flow.get("outcome_values") or [])
                        if isinstance(v, Mapping)][:12],
                    pre_hardening=pre_hardening,
                ))
                report["traversals"] += 1
                if completed and flow.get("outcome_values"):
                    drift_candidates.append((
                        journey.journey_id, traversal_id,
                        [v for v in (flow.get("outcome_values") or [])
                         if isinstance(v, Mapping)][:12],
                    ))

            # ── Nodes + edges + branches ──────────────────────────────────
            for i, step in enumerate(steps):
                fp = str(step.get("fingerprint") or "")
                if not fp:
                    continue
                is_last = i == len(steps) - 1
                dps = [d for d in (step.get("decision_points") or [])
                       if isinstance(d, Mapping)]
                node = (await session.execute(
                    select(JourneyNodeRow).where(
                        JourneyNodeRow.tenant_id == tenant_id,
                        JourneyNodeRow.app_id == app_id,
                        JourneyNodeRow.fingerprint == fp,
                    ))).scalar_one_or_none()
                if node is None:
                    node = JourneyNodeRow(
                        node_id=_sid("node", tenant_id, app_id, fp),
                        tenant_id=tenant_id, app_id=app_id, fingerprint=fp,
                    )
                    session.add(node)
                    report["nodes"] += 1
                node.url = str(step.get("url") or node.url or "")[:2000]
                node.title = str(step.get("title") or node.title or "")[:200]
                node.is_decision = bool(node.is_decision or dps)
                if is_last and terminal == "submit_boundary":
                    node.is_boundary = True
                if is_last and flow.get("outcome_values"):
                    node.has_outcome = True
                node.stale = False
                node.last_seen_at = now

                page_state = states_index.get(fp)
                if page_state:
                    new_controls = extract_controls(page_state, ledger_by_url)
                    if new_controls:
                        node.controls_inventory = merge_controls(
                            node.controls_inventory, new_controls)
                    new_outcomes = extract_outcomes(page_state)
                    if new_outcomes:
                        node.displayed_outcomes = merge_outcomes(
                            node.displayed_outcomes, new_outcomes)

                # Edge OUT of this step (the advance that left it).
                adv = step.get("advance")
                if isinstance(adv, Mapping) and not is_last:
                    to_fp = str(steps[i + 1].get("fingerprint") or "")
                    trigger = normalize_label(str(adv.get("control_name") or ""))
                    if to_fp and trigger:
                        edge = (await session.execute(
                            select(JourneyEdgeRow).where(
                                JourneyEdgeRow.tenant_id == tenant_id,
                                JourneyEdgeRow.app_id == app_id,
                                JourneyEdgeRow.from_fp == fp,
                                JourneyEdgeRow.to_fp == to_fp,
                                JourneyEdgeRow.trigger_label_norm == trigger,
                            ))).scalar_one_or_none()
                        if edge is None:
                            session.add(JourneyEdgeRow(
                                edge_id=_sid("edge", tenant_id, app_id, fp,
                                             to_fp, trigger),
                                tenant_id=tenant_id, app_id=app_id,
                                from_fp=fp, to_fp=to_fp,
                                trigger_label_norm=trigger,
                                advance_tier=int(adv.get("tier") or 0),
                                walk_count=1))
                            report["edges"] += 1
                        elif is_new_traversal:
                            edge.walk_count += 1
                            edge.last_walked_at = now
                            edge.advance_tier = int(adv.get("tier") or 0)

                # Branches: every enumerated option — walked or NOT.
                for dp in dps:
                    # A radio group is ONE question answered by N elements. Every
                    # member reports the same ``group_id``, so keying branches on
                    # it records N branches (one per answer) rather than an N×N
                    # cross-product of phantom decisions — and gives a planned
                    # walk a single stable key to force a choice on. Falls back to
                    # the control signature for genuinely single-element decision
                    # points (selects, checkboxes) and pre-group_id evidence.
                    sig = str(dp.get("group_id") or "") or str(
                        dp.get("control_signature") or "")
                    label = normalize_label(str(dp.get("control_label") or ""))
                    if not sig:
                        continue
                    choice = normalize_label(str(dp.get("choice") or ""))
                    for opt in dp.get("options") or []:
                        opt_norm = normalize_label(str(opt))
                        if not opt_norm:
                            continue
                        branch = (await session.execute(
                            select(JourneyBranchRow).where(
                                JourneyBranchRow.tenant_id == tenant_id,
                                JourneyBranchRow.app_id == app_id,
                                JourneyBranchRow.node_fp == fp,
                                JourneyBranchRow.control_signature == sig,
                                JourneyBranchRow.option_label_norm == opt_norm,
                            ))).scalar_one_or_none()
                        walked_now = bool(choice) and opt_norm == choice
                        if branch is None:
                            session.add(JourneyBranchRow(
                                branch_id=_sid("branch", tenant_id, app_id, fp,
                                               sig, opt_norm),
                                tenant_id=tenant_id, app_id=app_id, node_fp=fp,
                                control_signature=sig,
                                control_label_norm=label,
                                option_label_norm=opt_norm,
                                status=(BRANCH_WALKED if walked_now
                                        else BRANCH_DISCOVERED),
                                walked_in_traversal=(traversal_id
                                                     if walked_now else ""),
                                last_status_at=now))
                            report["branches"] += 1
                        elif walked_now and branch.status != BRANCH_WALKED:
                            # walked WINS — planned/blocked/discovered all
                            # upgrade; nothing ever downgrades walked.
                            branch.status = BRANCH_WALKED
                            branch.walked_in_traversal = traversal_id
                            branch.blocked_reason = ""
                            branch.last_status_at = now

    for j_id, t_id, o_vals in drift_candidates:
        try:
            result = await detect_drift(
                tenant_id=tenant_id, app_id=app_id, journey_id=j_id,
                new_traversal_id=t_id, new_outcome_values=o_vals)
            if result.get("action") in ("validated", "drifted"):
                report["drift_checks"] += 1
        except Exception as exc:
            logger.warning(
                "qec.journey_fold.drift_check_failed journey=%s err=%s",
                j_id, str(exc)[:200])

    logger.warning(
        "qec.journey_fold.folded tenant=%s app=%s exploration=%s "
        "flows=%d journeys=%d nodes=%d edges=%d traversals=%d branches=%d "
        "drift_checks=%d",
        tenant_id, app_id, exploration_id, report["flows"], report["journeys"],
        report["nodes"], report["edges"], report["traversals"],
        report["branches"], report["drift_checks"])
    return report
