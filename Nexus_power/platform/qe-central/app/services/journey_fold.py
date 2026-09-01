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
    apply_control_lifecycle, build_ledger_by_url, build_states_index,
    crawl_evidence, extract_controls, extract_outcomes,
    merge_controls, merge_outcomes, observed_question_ids, question_id_for,
    RETIREMENT_MISS_THRESHOLD,
)
from .catalog_store import persist_catalog_version
from .journey_criticality_store import persist_criticality_bands
from .endpoint_map import merge_endpoints
from .journey_baseline import BASELINE_CAPTURED, detect_drift
from . import endpoint_map

logger = logging.getLogger(__name__)

#: Branch statuses (journey_branches.status).
BRANCH_WALKED = "walked"
BRANCH_DISCOVERED = "discovered"
BRANCH_PLANNED = "planned"
BRANCH_BLOCKED = "blocked"


def merge_reveals(existing: Any, new: Any) -> list[str]:
    """Union of value-free trigger→child reveal identities (P1), order-preserving.

    Called when the WALKED option of a decision point recorded what it activated.
    Merges across crawls so a base crawl's "No" reveals and a planned re-crawl's
    "Yes" reveals both accumulate on their own branch rows. Idempotent + capped.
    """
    out = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = set(out)
    for x in (new or ()):
        s = str(x)[:80]
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:128]
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
#:
#: ``submit_crossed`` (A4.3) and ``confirmation`` (M1.4) joined that set on the
#: producer side. A mirror that silently stops mirroring is worse than no
#: mirror: the fallback would have read a completed journey as truncated on
#: exactly the rows it exists to rescue.
_COMPLETING_TERMINALS = frozenset({"submit_boundary", "no_advance",
                                   "submit_crossed", "confirmation"})


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
              "traversals": 0, "branches": 0, "drift_checks": 0,
              "questions_retired": 0, "questions_stale": 0, "nodes_stale": 0}
    if not flows:
        return report
    pre_hardening = is_pre_hardening(coverage)
    now = utc_now()
    drift_candidates: list[tuple[str, str, list]] = []

    states_index = build_states_index(coverage)
    ledger_by_url = build_ledger_by_url(coverage)
    # M2.4 / T-GEN-03 — the M2.5 endpoint inventory, inverted to {UI action
    # label: [endpoint]}. Built ONCE per crawl rather than per edge: the
    # inventory is crawl-level and re-inverting it inside the step loop would
    # repeat the same work once per transition of every journey.
    endpoints_by_action = endpoint_map.inventory_by_action(
        (coverage or {}).get("endpoint_inventory")
        if isinstance(coverage, Mapping) else None)

    # ── M2.3 · WHAT THIS CRAWL ACTUALLY OBSERVED ────────────────────────────
    # Retirement needs the other half of the ledger: not only what was seen, but
    # WHERE the crawl looked and came back empty. ``observed_by_fp`` is that —
    # per state, the question ids that state asked, derived through the same
    # ``extract_controls``/``question_id_for`` pair the catalogue is built with,
    # so "observed here" and "catalogued here" are the same identity rather than
    # two rules that can drift apart and retire a question that never moved.
    #
    # Only states in ``states_index`` are eligible. A node the crawl walked but
    # recorded no signals for is a node whose inventory we did not read, and an
    # unread page is not a page whose questions are gone.
    observed_by_fp: dict[str, set[str]] = {
        fp: observed_question_ids(state, ledger_by_url)
        for fp, state in states_index.items()
    }
    # …AND THE SAME THING KEYED BY URL, which is what makes retirement possible
    # at all. A node's identity is a STATE identity: removing a question from a
    # page changes the page's accessibility structure and therefore changes its
    # fingerprint. Measured on two real crawls of ``proving-grounds/acme-life``
    # either side of a real removal: the application form's fingerprint moved
    # from b2111159… to fa9c56c9… while its URL did not move at all. Keyed only
    # by fingerprint, the node holding the removed question is never re-observed,
    # its inventory is never re-examined, and the question can never retire —
    # precisely for the change retirement exists to catch. See
    # :func:`_apply_lifecycle` for how the two are used together.
    observed_by_url: dict[str, set[str]] = {}
    for state in states_index.values():
        loc = str(state.get("location") or "")
        if loc:
            observed_by_url.setdefault(loc, set()).update(
                observed_question_ids(state, ledger_by_url))
    #: Per node, the (control signature, option) pairs this crawl enumerated —
    #: the branch-row equivalent of the above. A questionnaire question lives as
    #: branch rows, so without this a withdrawn Yes/No could never retire.
    observed_branches: dict[str, set[tuple[str, str]]] = {}
    #: Nodes this crawl actually stepped through. Distinct from ``states_index``:
    #: a state may be recorded without any walk enumerating its decisions, and
    #: only a WALKED node licenses a conclusion about its branches.
    walked_fps: set[str] = set()
    evidence = crawl_evidence(coverage)

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
                walked_fps.add(fp)
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
                    # M2.4 / T-GEN-03 — the state's own endpoint map. Merged
                    # rather than replaced: a re-crawl that happens not to
                    # exercise a call must not erase the evidence that the call
                    # exists, which is the same rule the control inventory and
                    # the displayed outcomes already follow.
                    new_endpoints = endpoint_map.endpoints_of(page_state)
                    if new_endpoints:
                        node.observed_endpoints = merge_endpoints(
                            node.observed_endpoints, new_endpoints)

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
                        # M2.4 / T-GEN-03 — WHICH CALLS THIS CLICK MADE. The
                        # crawl stamped the in-flight UI action on every network
                        # event (M2.5 / T-NET-03); the inventory carries those
                        # forward per endpoint, and this is the join. An empty
                        # result is honest and common — a crawl that predates
                        # the stamp has nothing to join — and the compiler then
                        # falls back to differencing the two states.
                        caused = endpoints_by_action.get(trigger) or []
                        if edge is None:
                            session.add(JourneyEdgeRow(
                                edge_id=_sid("edge", tenant_id, app_id, fp,
                                             to_fp, trigger),
                                tenant_id=tenant_id, app_id=app_id,
                                from_fp=fp, to_fp=to_fp,
                                trigger_label_norm=trigger,
                                advance_tier=int(adv.get("tier") or 0),
                                observed_endpoints=(list(caused) or None),
                                walk_count=1))
                            report["edges"] += 1
                        else:
                            if caused:
                                edge.observed_endpoints = merge_endpoints(
                                    edge.observed_endpoints, caused)
                            if is_new_traversal:
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
                    # M2.3 — this walk enumerated these answers HERE. Recorded
                    # before the option loop so an option the application has
                    # withdrawn (absent from ``options`` and so never reaching a
                    # branch upsert) is still measurable as an absence.
                    seen_here = observed_branches.setdefault(fp, set())
                    for _opt in dp.get("options") or []:
                        _opt_norm = normalize_label(str(_opt))
                        if _opt_norm:
                            seen_here.add((sig, _opt_norm))
                    choice = normalize_label(str(dp.get("choice") or ""))
                    # A next-action fork classifies each option. A destructive
                    # ("Start Over" wipes the quote) or navigational ("Back to
                    # Dashboard" leaves the funnel) option must be surfaced in the
                    # catalogue but NEVER queued for a walk — branch_planner only
                    # plans `discovered` branches, so blocking them here is what
                    # keeps the planner from clicking "Start Over". Absent (a field
                    # decision) → behave exactly as before.
                    raw_classes = dp.get("option_classes")
                    option_classes = raw_classes if isinstance(raw_classes, dict) else {}
                    # P1 trigger→child: what the WALKED option activated. Only the
                    # taken option (choice) carries reveals — it attaches below to
                    # that option's branch row.
                    dp_reveals = dp.get("reveals")
                    if not isinstance(dp_reveals, (list, tuple)):
                        dp_reveals = None
                    for opt in dp.get("options") or []:
                        opt_norm = normalize_label(str(opt))
                        if not opt_norm:
                            continue
                        cls = str(option_classes.get(opt) or "").strip().lower()
                        blocked_reason = ""
                        if cls == "destructive":
                            blocked_reason = ("destructive next-action (irreversible) "
                                              "— surfaced, not walked")
                        elif cls == "navigational":
                            blocked_reason = ("navigational next-action — exits the "
                                              "funnel, surfaced not walked")
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
                            if walked_now:
                                init_status = BRANCH_WALKED
                            elif blocked_reason:
                                init_status = BRANCH_BLOCKED
                            else:
                                init_status = BRANCH_DISCOVERED
                            branch = JourneyBranchRow(
                                branch_id=_sid("branch", tenant_id, app_id, fp,
                                               sig, opt_norm),
                                tenant_id=tenant_id, app_id=app_id, node_fp=fp,
                                control_signature=sig,
                                control_label_norm=label,
                                option_label_norm=opt_norm,
                                status=init_status,
                                blocked_reason=(blocked_reason
                                                if init_status == BRANCH_BLOCKED else ""),
                                walked_in_traversal=(traversal_id
                                                     if walked_now else ""),
                                last_status_at=now)
                            session.add(branch)
                            report["branches"] += 1
                        # THE WORDING IS RE-READ ON EVERY FOLD, THE KEY NEVER IS
                        # (M2.1). ``control_label_norm`` is set once at row
                        # creation, so a branch first recorded before the crawl
                        # could read a question's text - or recorded under an
                        # answer's name, which is what it held before this
                        # milestone - kept that label for ever, and no re-crawl
                        # could correct it. It is product UI text, so the latest
                        # observation is simply the truest one.
                        #
                        # Safe precisely because identity does NOT depend on it:
                        # the row is keyed on (node, control_signature, option)
                        # and the catalogue question id is derived from the
                        # signature, so re-reading a rewording updates the words
                        # and moves nothing. Only ever upgrades - a fold that
                        # observed no label never blanks one already read.
                        if branch is not None and label and branch.control_label_norm != label:
                            branch.control_label_norm = label
                        if walked_now and branch.status != BRANCH_WALKED:
                            # walked WINS — planned/blocked/discovered all
                            # upgrade; nothing ever downgrades walked.
                            branch.status = BRANCH_WALKED
                            branch.walked_in_traversal = traversal_id
                            branch.blocked_reason = ""
                            branch.last_status_at = now
                        # P1: accumulate what the walked option revealed (union
                        # across crawls). Only the taken option carries reveals.
                        if walked_now and dp_reveals and branch is not None:
                            branch.reveals = merge_reveals(branch.reveals, dp_reveals)

        # ── M2.3 · THE LIFECYCLE PASS ───────────────────────────────────────
        # Runs after every node, edge and branch of this crawl is upserted, in
        # the SAME transaction, so what it marks absent is measured against the
        # graph this crawl just finished writing rather than against a snapshot
        # that could still change under it.
        lifecycle = await _apply_lifecycle(
            session, tenant_id=tenant_id, app_id=app_id,
            crawl_ref=exploration_id, now=now,
            observed_by_fp=observed_by_fp,
            observed_by_url=observed_by_url,
            observed_branches=observed_branches,
            walked_fps=walked_fps,
            conclusive=bool(evidence["conclusive"]),
        )
        report.update(lifecycle)

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

    # P2/P6: refresh the durable Master Catalog + snapshot a version for
    # regression diffing (keyed by this crawl's exploration id). Best-effort —
    # a catalog failure must never break the fold.
    try:
        cat_report = await persist_catalog_version(
            tenant_id=tenant_id, app_id=app_id, crawl_ref=exploration_id)
        report["catalog_questions"] = cat_report.get("questions_upserted", 0)
    except Exception as exc:
        logger.warning(
            "qec.journey_fold.catalog_persist_failed tenant=%s app=%s err=%s",
            tenant_id, app_id, str(exc)[:200])

    # Tier 2: band every journey on the evidence this fold just committed and
    # STORE the verdict (qec_025), so the next crawl can say which journeys
    # changed criticality. Best-effort for the same reason the catalogue refresh
    # above is: the graph, the traversals and the catalogue are all committed,
    # and the band is an annotation on top of them. The ranked API surface keeps
    # evaluating live and is unaffected either way.
    try:
        band_report = await persist_criticality_bands(
            tenant_id=tenant_id, app_id=app_id, crawl_ref=exploration_id)
        report["journeys_banded"] = band_report["banded"]
        report["criticality_changed"] = band_report["changed"]
    except Exception as exc:
        logger.warning(
            "qec.journey_fold.criticality_persist_failed tenant=%s app=%s err=%s",
            tenant_id, app_id, str(exc)[:200])

    logger.warning(
        "qec.journey_fold.folded tenant=%s app=%s exploration=%s "
        "flows=%d journeys=%d nodes=%d edges=%d traversals=%d branches=%d "
        "drift_checks=%d retired=%d stale=%d nodes_stale=%d "
        "conclusive=%s evidence=%s",
        tenant_id, app_id, exploration_id, report["flows"], report["journeys"],
        report["nodes"], report["edges"], report["traversals"],
        report["branches"], report["drift_checks"],
        report.get("questions_retired", 0), report.get("questions_stale", 0),
        report.get("nodes_stale", 0), evidence["conclusive"],
        evidence["reason"] or "-")
    return report


#: How much of a node's known question set a state at the SAME URL must share
#: before it counts as that node's replacement. Half.
#:
#: The number exists to separate two things a URL alone cannot tell apart: a page
#: that CHANGED (most of its questions are still there, one is gone) from a
#: DIFFERENT STEP of a single-page application that happens to serve every step
#: from one URL. Supersede on the first and a removal is caught; supersede on the
#: second and a crawl that reached step 1 of a twenty-step wizard would retire
#: every question in steps 2 to 20 — the exact catastrophe a lifecycle feature
#: must not be able to cause.
#:
#: A node sharing NOTHING with what the crawl saw at its URL is left entirely
#: alone: the crawl was somewhere else, and that is an absence of evidence.
SUPERSEDE_OVERLAP_RATIO = 0.5


def _node_question_ids(node: Any) -> set[str]:
    """The question ids one node's stored inventory holds."""
    out: set[str] = set()
    for ctrl in (node.controls_inventory or []):
        if isinstance(ctrl, Mapping):
            qid = str(ctrl.get("question_id") or "") or question_id_for(ctrl)
            if qid:
                out.add(qid)
    return out


def _superseding_observation(
    node: Any, observed_by_url: Mapping[str, set[str]],
) -> set[str] | None:
    """What this crawl saw at the node's URL, IF it replaced this node.

    Returns the observed question set when the crawl read a state at the same URL
    that shares at least :data:`SUPERSEDE_OVERLAP_RATIO` of this node's known
    questions — evidence that it was looking at this page in its new shape.
    Returns None when it was not, and the caller then concludes nothing.
    """
    known = _node_question_ids(node)
    if not known:
        return None
    seen = observed_by_url.get(str(node.url or ""))
    if not seen:
        return None
    overlap = len(known & seen)
    needed = max(1, int(len(known) * SUPERSEDE_OVERLAP_RATIO + 0.999))
    return seen if overlap >= needed else None


async def _apply_lifecycle(
    session: Any, *, tenant_id: str, app_id: str, crawl_ref: str, now: Any,
    observed_by_fp: Mapping[str, set[str]],
    observed_by_url: Mapping[str, set[str]],
    observed_branches: Mapping[str, set[tuple[str, str]]],
    walked_fps: set[str],
    conclusive: bool,
) -> dict[str, int]:
    """Mark what this crawl LOOKED FOR and did not find. Deletes nothing.

    Three passes, each over a different kind of absence, and each refusing to
    conclude anything from a page the crawl did not read:

      1. **Controls on re-read pages.** For every state this crawl recorded, the
         node's control inventory is stamped against what that state actually
         asked. A control missing from a page the crawl DID read is an evidenced
         absence; :func:`catalog.apply_control_lifecycle` decides whether it is
         yet conclusive enough to retire.

      1b. **Controls on SUPERSEDED pages.** A node is a STATE, and removing a
         question changes a page's structure and so its fingerprint — the node
         holding the removed question is therefore never re-observed by the crawl
         that proves it gone. Where the crawl read a state at the SAME URL that
         still shares most of this node's questions, that state IS this page in
         its new shape, and the node's inventory is stamped against it. Without
         this the whole feature is inert for the commonest kind of change there
         is; with it unguarded, one crawl of one wizard step could retire a whole
         application. See :data:`SUPERSEDE_OVERLAP_RATIO`.
      2. **Branches on re-walked pages.** The same, one row per ANSWER, for the
         questionnaire questions that live as branch rows. Gated on the node
         having been WALKED as well as recorded: a state that was captured but
         whose decisions were never enumerated tells us nothing about its
         answers, and treating that silence as removal would retire every choice
         question in the application the first time a walk took another path.
      3. **Nodes.** ``journey_nodes.stale`` has carried a docstring promising
         "not observed by the app's latest fold — kept, marked, and excluded
         from active planning" since qec_005, and had exactly one writer, which
         assigned it ``False``. This is the writer that makes the column true.
         It fires only for a CONCLUSIVE crawl: a crawl that stopped on a budget
         or never got past a login wall has not visited the pages it missed, and
         staling them would mark most of an application dead on every short run.

    ``status`` on a branch is deliberately untouched — ``walked`` is a fact about
    a crawl that happened and must never downgrade. Retirement is a statement
    about the application TODAY, and lives on its own axis.
    """
    now_iso = now.isoformat() if hasattr(now, "isoformat") else str(now)
    counts = {"questions_retired": 0, "questions_stale": 0, "nodes_stale": 0}

    node_rows = (await session.execute(
        select(JourneyNodeRow).where(
            JourneyNodeRow.tenant_id == tenant_id,
            JourneyNodeRow.app_id == app_id,
        ))).scalars().all()

    for node in node_rows:
        fp = str(node.fingerprint or "")
        superseded = (None if fp in observed_by_fp
                      else _superseding_observation(node, observed_by_url))
        if fp in observed_by_fp or superseded is not None:
            observed = (observed_by_fp[fp] if fp in observed_by_fp else superseded)
            before = list(node.controls_inventory or [])
            after = apply_control_lifecycle(
                before, observed, crawl_ref=crawl_ref,
                now_iso=now_iso, conclusive=conclusive)
            node.controls_inventory = after
            # A SUPERSEDED NODE IS STILL GONE. Its questions were re-examined
            # against the page's new shape, but this exact state was not seen and
            # must not be reported as current — the node stays stale and only its
            # replacement is active.
            if superseded is None:
                node.stale = False
                node.last_seen_at = now
            else:
                node.stale = True
                counts["nodes_stale"] += 1
            for entry in after:
                if entry.get("retired_at") and entry.get("retired_in_crawl") == crawl_ref:
                    counts["questions_retired"] += 1
                elif entry.get("stale"):
                    counts["questions_stale"] += 1
        elif conclusive and not node.stale:
            # The application was observed end to end and this state was not in
            # it. Marked, kept, never deleted — and revived the moment a later
            # crawl reaches it again.
            node.stale = True
            counts["nodes_stale"] += 1

    # Branches: only where the crawl both RECORDED and WALKED the node.
    branch_fps = {fp for fp in observed_branches if fp in walked_fps}
    if branch_fps:
        branch_rows = (await session.execute(
            select(JourneyBranchRow).where(
                JourneyBranchRow.tenant_id == tenant_id,
                JourneyBranchRow.app_id == app_id,
                JourneyBranchRow.node_fp.in_(sorted(branch_fps)),
            ))).scalars().all()
        for branch in branch_rows:
            key = (str(branch.control_signature or ""),
                   str(branch.option_label_norm or ""))
            seen = observed_branches.get(str(branch.node_fp or ""), set())
            if key in seen:
                branch.stale = False
                branch.missed_crawls = 0
                branch.retired_at = None
                branch.retired_in_crawl = ""
                branch.retire_reason = ""
                branch.last_seen_crawl = crawl_ref[:64]
                continue
            branch.stale = True
            branch.missed_crawls = int(branch.missed_crawls or 0) + 1
            if branch.retired_at is None:
                if conclusive:
                    branch.retired_at = now
                    branch.retired_in_crawl = crawl_ref[:64]
                    branch.retire_reason = "conclusive_absence"
                    counts["questions_retired"] += 1
                elif branch.missed_crawls >= RETIREMENT_MISS_THRESHOLD:
                    branch.retired_at = now
                    branch.retired_in_crawl = crawl_ref[:64]
                    branch.retire_reason = "repeated_absence"
                    counts["questions_retired"] += 1
                else:
                    counts["questions_stale"] += 1
            else:
                counts["questions_stale"] += 1

    return counts
