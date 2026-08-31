"""P2/P6 — Master Catalog persistence + regression store (the DB layer).

The pure catalog logic lives in ``catalog.py`` (build_master_catalog,
snapshot_catalog) and ``catalog_diff.py`` (diff_catalogs). This module is the
thin DB seam that:

  * ``build_app_master_catalog`` — aggregates the app's journey nodes/edges into
    the live Master Catalog for the read route (no persistence needed);
  * ``persist_catalog_version`` — at fold, upserts ``catalog_questions`` (the
    durable, deduped master) and writes a ``catalog_versions`` snapshot so a later
    crawl can diff against it (P6). Best-effort: never breaks the fold;
  * ``diff_latest_versions`` — loads the two most recent versions and diffs them.

All queries run inside ``tenant_scoped_qec_session`` (RLS-forced), so nothing
crosses a tenant boundary.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from sqlalchemy import select

from ..db import tenant_scoped_qec_session, utc_now
from ..db.journey_models import (
    CatalogQuestionRow,
    CatalogVersionRow,
    JourneyBranchRow,
    JourneyEdgeRow,
    JourneyNodeRow,
)
from .catalog import (
    LIFECYCLE_RETIRED, MAX_CATALOG_OPTIONS, build_master_catalog,
    snapshot_catalog,
)
from .catalog_diff import diff_catalogs

logger = logging.getLogger(__name__)


def _sid(*parts: str) -> str:
    """Deterministic row id from a natural key — the upsert identity."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:64]


async def _load_rules(tenant_id: str, app_id: str) -> list[dict]:
    """Every business rule this tenant has PROVED about this app.

    Read through :func:`rule_store.fetch_rules` rather than with a query of its
    own, so the catalogue and the dispatch that hands rules back to a crawl see
    exactly the same rows under exactly the same schema-version filter. A second
    query here would be a second definition of "a rule this app has", and the day
    they disagreed the catalogue would claim evidence the engine had rejected.

    Fails open to ``[]`` inside ``fetch_rules``: an unreachable rule store must
    cost the catalogue its rules — which it then reports honestly as UNVERIFIED —
    never the catalogue itself.
    """
    from .rule_store import fetch_rules
    return await fetch_rules(tenant_id, app_id)


async def _load_graph(
    session: Any, tenant_id: str, app_id: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Every node (with its control inventory), edge, and branch for the app —
    branches carry the questionnaire questions the Master Catalog folds in."""
    node_rows = (await session.execute(
        select(JourneyNodeRow).where(
            JourneyNodeRow.tenant_id == tenant_id,
            JourneyNodeRow.app_id == app_id,
        ))).scalars().all()
    edge_rows = (await session.execute(
        select(JourneyEdgeRow).where(
            JourneyEdgeRow.tenant_id == tenant_id,
            JourneyEdgeRow.app_id == app_id,
        ))).scalars().all()
    branch_rows = (await session.execute(
        select(JourneyBranchRow).where(
            JourneyBranchRow.tenant_id == tenant_id,
            JourneyBranchRow.app_id == app_id,
        ))).scalars().all()
    nodes = [{
        "node_fp": n.fingerprint,
        "url": n.url,
        "title": n.title,
        "controls_inventory": list(n.controls_inventory or []),
    } for n in node_rows]
    edges = [{"from_fp": e.from_fp, "to_fp": e.to_fp} for e in edge_rows]
    branches = [{
        "node_fp": b.node_fp,
        "control_signature": b.control_signature,
        "control_label_norm": b.control_label_norm,
        "option_label_norm": b.option_label_norm,
        # reveals is what makes a branch a trigger→child RULE — without it
        # rules_from_branches produces nothing and no journey ever branches.
        "reveals": list(b.reveals or []),
        # M2.3 — the answer's lifecycle. Carried into the catalogue build so a
        # question the application withdrew is not resurrected as active by the
        # branch fold-in the moment the node side retired it.
        "stale": bool(b.stale),
        "retired_at": b.retired_at.isoformat() if b.retired_at else "",
        "retired_in_crawl": b.retired_in_crawl or "",
        "retire_reason": b.retire_reason or "",
        "last_seen_crawl": b.last_seen_crawl or "",
    } for b in branch_rows]
    return nodes, edges, branches


def _reveal_rules_fn(branches: Any):
    """The trigger->child resolver, bound to one app's branch rows.

    ``build_master_catalog`` cannot import ``journey_projector`` — that module
    imports ``question_id_for`` and ``group_question_id`` FROM the catalogue, so
    the dependency only runs one way and must keep doing so. And the rules cannot
    be computed before the catalogue exists, because resolving a reveal identity
    to a child question needs the questions. A closure is what satisfies both:
    the catalogue calls back once its rows are assembled, and the ONE id space
    both sides share stays the one ``question_id_for`` defines.
    """
    from .journey_projector import rules_from_branches
    return lambda questions: rules_from_branches(branches, questions)


async def build_app_master_catalog(
    tenant_id: str, app_id: str, include_retired: bool = False,
) -> dict[str, Any]:
    """The live app-scoped Master Catalog, aggregated from the journey graph.

    Read path for ``GET /apps/{app_id}/catalog`` — deduped by stable question_id
    across every journey/node, so the 400 questions appear once.

    ``include_retired`` selects the AUDIT view: every question this application
    has ever asked, each labelled with its lifecycle and — where it retired — the
    timestamp and the crawl that retired it. The default is the ACTIVE catalogue,
    which is what planning and scenario derivation must read, because a plan
    built against a question the application no longer asks is a plan that fails
    for a reason the report will blame on the application.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        nodes, edges, branches = await _load_graph(session, tenant_id, app_id)
    rules = await _load_rules(tenant_id, app_id)
    return build_master_catalog(nodes, edges=edges, branches=branches, rules=rules,
                                include_retired=include_retired,
                                reveal_rules_fn=_reveal_rules_fn(branches))


async def load_catalog_and_rules(
    tenant_id: str, app_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The Master Catalog + the trigger→child rules built from branch reveals.

    The pair the persona journey projector needs: ``build_master_catalog`` (with
    questionnaire questions folded in) and ``rules_from_branches`` over the same
    branch rows, in one id space.
    """
    from .journey_projector import rules_from_branches
    async with tenant_scoped_qec_session(tenant_id) as session:
        nodes, edges, branches = await _load_graph(session, tenant_id, app_id)
    # Two different things both called "rules", deliberately kept apart: the
    # BUSINESS rules an experiment proved (what a question requires) go INTO the
    # catalogue, while the trigger→child branch rules the projector returns are
    # derived FROM it (which question reveals which). Folding either into the
    # other would let a projection rule masquerade as observed business logic.
    business_rules = await _load_rules(tenant_id, app_id)
    # The rules are captured on the way through rather than recomputed after:
    # the catalogue resolves them against its own rows, and computing them a
    # second time against the same rows to hand to the projector would be two
    # chances to disagree about one join.
    captured: list[dict[str, Any]] = []

    def _fn(questions):
        rules = rules_from_branches(branches, questions)
        captured[:] = rules
        return rules

    master = build_master_catalog(nodes, edges=edges, branches=branches,
                                  rules=business_rules, reveal_rules_fn=_fn)
    return master, captured


async def persist_catalog_version(
    tenant_id: str, app_id: str, crawl_ref: str
) -> dict[str, Any]:
    """Upsert ``catalog_questions`` and snapshot a ``catalog_versions`` row.

    Called once at the end of a fold so the Master Catalog is durable and a later
    crawl can diff against this version. Idempotent per (tenant, app, question_id)
    and (tenant, app, crawl_ref). Returns a small report.

    ``crawl_ref`` IS THE EXPLORATION ID, not an ``canonical_artifacts.artifact_id``.
    One catalog version per CRAWL is the intended grain — every crawl of an app
    reuses one artifact (the dedup key deliberately excludes crawl_id so versions
    accumulate there), so keying catalog versions by artifact would collapse every
    crawl of an app onto a single row and there would be nothing to diff. The
    parameter was previously named ``artifact_id`` while receiving an exploration
    id, and the columns it writes still carry the older names
    (``first_seen_artifact`` / ``last_seen_artifact`` / ``catalog_versions.
    artifact_id``) because live rows hold those values; renaming the columns is a
    migration, and a name is not worth rewriting durable data over. What matters
    is that nothing JOINS these against ``canonical_artifacts`` — they are crawl
    references, and are surfaced as ``crawl_ref`` by :func:`diff_latest_versions`.
    """
    now = utc_now()
    rules = await _load_rules(tenant_id, app_id)
    async with tenant_scoped_qec_session(tenant_id) as session:
        nodes, edges, branches = await _load_graph(session, tenant_id, app_id)
        # BOTH CATALOGUES, ONE BUILD. ``full`` carries every question the
        # application has ever asked, retired ones included and labelled;
        # ``questions`` is the active subset. The durable rows are stamped from
        # ``full`` — a retired question still needs its row updated, that is what
        # records the retirement — while the SNAPSHOT is taken from the active
        # catalogue alone, and that is precisely what makes ``catalog_diff``'s
        # ``removed`` bucket reachable: the retired question leaves the new
        # snapshot, so the next diff names it.
        full = build_master_catalog(nodes, edges=edges, branches=branches,
                                    rules=rules, include_retired=True,
                                    reveal_rules_fn=_reveal_rules_fn(branches))
        all_questions = full.get("questions") or []
        # NO SUMMARY IS CARRIED ONTO THIS. ``snapshot_catalog`` reads only
        # ``questions``, and ``full``'s summary counts the retired rows too —
        # copying it here would put an over-count on an object whose whole
        # purpose is to be the ACTIVE catalogue. The lifecycle numbers this
        # function reports are computed from the two lists below instead.
        master = {"questions": [q for q in all_questions
                                if q.get("lifecycle") != LIFECYCLE_RETIRED]}
        questions = master["questions"]
        retired_now = 0

        upserted = 0
        for q in all_questions:
            qid = str(q.get("question_id") or "")
            if not qid:
                continue
            # ── M2.3 · THE LIFECYCLE THIS BUILD RESOLVED ────────────────────
            is_retired = q.get("lifecycle") == LIFECYCLE_RETIRED
            q_stale = bool(q.get("stale"))
            retired_at = _parse_ts(q.get("retired_at")) if is_retired else None
            row = (await session.execute(
                select(CatalogQuestionRow).where(
                    CatalogQuestionRow.tenant_id == tenant_id,
                    CatalogQuestionRow.app_id == app_id,
                    CatalogQuestionRow.question_id == qid,
                ))).scalar_one_or_none()
            pages = [str(p) for p in (q.get("pages") or [])][:64]
            validation = q.get("validation") if isinstance(q.get("validation"), dict) else None
            # ── M2.2 — the four signals qec_019 made durable ─────────────────
            locator = q.get("locator") if isinstance(q.get("locator"), dict) else None
            rule_evidence = (q.get("business_rule_evidence")
                             if isinstance(q.get("business_rule_evidence"), dict)
                             else None)
            depends_on = str(q.get("depends_on") or "")[:200] or None
            depends_on_source = str(q.get("depends_on_source") or "")[:16]
            revealed_by = (list(q.get("revealed_by") or [])
                           if isinstance(q.get("revealed_by"), list) else None)
            try:
                options_total = int(q.get("options_total") or 0)
            except (TypeError, ValueError):
                options_total = 0
            rule_state = str(q.get("business_rule_state") or "UNVERIFIED")[:24]
            if row is None:
                session.add(CatalogQuestionRow(
                    cq_id=_sid("cq", tenant_id, app_id, qid),
                    tenant_id=tenant_id, app_id=app_id, question_id=qid,
                    name=str(q.get("name") or "")[:300],
                    answer_type=str(q.get("type") or q.get("answer_type") or "text")[:40],
                    required=bool(q.get("required")),
                    options=[str(o) for o in (q.get("options") or [])][:MAX_CATALOG_OPTIONS],
                    validation=validation,
                    business_rule=str(q.get("business_rule") or "")[:500],
                    business_rule_state=rule_state,
                    business_rule_evidence=rule_evidence,
                    depends_on=depends_on,
                    depends_on_source=depends_on_source,
                    revealed_by=revealed_by,
                    locator=locator,
                    options_total=max(options_total,
                                      len(q.get("options") or [])),
                    expected_next_page=str(q.get("expected_next_page") or "")[:200],
                    semantic_type=str(q.get("semantic_type") or "")[:80],
                    provenance=str(q.get("provenance") or "observed")[:24],
                    pages=pages,
                    first_seen_artifact=crawl_ref[:64],
                    last_seen_artifact=crawl_ref[:64],
                    first_seen_at=now, last_seen_at=now,
                    stale=q_stale,
                    missed_crawls=int(q.get("missed_crawls") or 0),
                    retired_at=retired_at,
                    retired_in_crawl=str(q.get("retired_in_crawl") or "")[:64],
                    retire_reason=str(q.get("retire_reason") or "")[:32],
                    last_seen_crawl=("" if is_retired else crawl_ref[:64]),
                ))
                upserted += 1
                if is_retired:
                    retired_now += 1
            else:
                # Keep the richest: required sticky, fill options/validation, merge pages.
                row.required = bool(row.required) or bool(q.get("required"))
                if not row.options and q.get("options"):
                    # The SAME ceiling the insert path uses. This was a bare 48,
                    # so a question first seen without options and filled in on a
                    # later crawl kept 48 answers while the identical question
                    # inserted with options kept 300 — the same question, two
                    # different answer sets, decided by which crawl saw it first.
                    row.options = [str(o) for o in q["options"]][:MAX_CATALOG_OPTIONS]
                if row.validation is None and validation:
                    row.validation = validation
                # ── M2.2 upsert semantics ────────────────────────────────────
                # A rule, a dependency and a locator are all things ONE sighting
                # proves and the others simply never witnessed — a dependent
                # select looks unconditional until the crawl that answers its
                # driver, and an experiment only runs where the app blocks. So
                # each of these fills in and UPGRADES, and none of them clears:
                # a later crawl that did not reach the gate must not erase the
                # evidence an earlier one produced. What DOES overwrite is a
                # fresher proof of the same kind, because the newest observation
                # is the one that describes the application as it is now.
                if q.get("business_rule"):
                    row.business_rule = str(q["business_rule"])[:500]
                    row.business_rule_state = rule_state
                    row.business_rule_evidence = rule_evidence
                elif not row.business_rule:
                    row.business_rule_state = rule_state
                if depends_on:
                    # FILL AND UPGRADE, NEVER CLEAR — the same M2.2 upsert rule
                    # the rule and the locator follow. A crawl that did not walk
                    # the trigger sees an unconditional-looking question and must
                    # not erase what a crawl that DID walk it proved.
                    row.depends_on = depends_on
                    row.depends_on_source = depends_on_source
                if revealed_by:
                    row.revealed_by = revealed_by
                if locator and (not row.locator
                                or (locator.get("verified")
                                    and not (row.locator or {}).get("verified"))):
                    row.locator = locator
                # NEVER DOWNWARD. The total is a count of what the application
                # offers; a later sighting of a dependent control that was still
                # empty is not evidence the answer set shrank.
                row.options_total = max(int(row.options_total or 0), options_total,
                                        len(row.options or []))
                if q.get("expected_next_page"):
                    row.expected_next_page = str(q["expected_next_page"])[:200]
                merged_pages = list(row.pages or [])
                for p in pages:
                    if p not in merged_pages:
                        merged_pages.append(p)
                row.pages = merged_pages[:64]

                # ── M2.3 · THE LIFECYCLE WRITE ───────────────────────────────
                # ``last_seen_artifact`` USED TO BUMP UNCONDITIONALLY, on every
                # fold, for every question in the catalogue — including ones this
                # crawl never saw. That is what made "when did we last actually
                # observe this question?" unanswerable, and it is why a removed
                # question looked freshly-confirmed for ever. It now bumps only
                # on a real sighting; a retired question keeps the crawl that
                # last saw it, which is the whole point of the record.
                if not q_stale and not is_retired:
                    row.stale = False
                    row.missed_crawls = 0
                    row.retired_at = None
                    row.retired_in_crawl = ""
                    row.retire_reason = ""
                    row.last_seen_crawl = crawl_ref[:64]
                    row.last_seen_artifact = crawl_ref[:64]
                    row.last_seen_at = now
                else:
                    row.stale = True
                    # MIRRORED, not incremented. The count belongs to the
                    # sightings — a fold that never visited the page did not miss
                    # the question, and incrementing here once per fold inflated
                    # the evidence trail with folds that never looked. Floored at
                    # what the row already holds so a rebuild cannot lose history.
                    row.missed_crawls = max(int(row.missed_crawls or 0),
                                            int(q.get("missed_crawls") or 0))
                    if is_retired and row.retired_at is None:
                        # FIRST retirement wins the timestamp. A later fold must
                        # not keep pushing the date forward — an auditor asking
                        # "when did this application stop asking?" is owed the
                        # crawl that established it, not the most recent one to
                        # agree.
                        row.retired_at = retired_at or now
                        row.retired_in_crawl = (
                            str(q.get("retired_in_crawl") or crawl_ref)[:64])
                        row.retire_reason = str(
                            q.get("retire_reason") or "conclusive_absence")[:32]
                        retired_now += 1

        snap = snapshot_catalog(master, artifact_id=crawl_ref)
        version = (await session.execute(
            select(CatalogVersionRow).where(
                CatalogVersionRow.tenant_id == tenant_id,
                CatalogVersionRow.app_id == app_id,
                CatalogVersionRow.artifact_id == crawl_ref,
            ))).scalar_one_or_none()
        if version is None:
            session.add(CatalogVersionRow(
                version_id=_sid("catver", tenant_id, app_id, crawl_ref),
                tenant_id=tenant_id, app_id=app_id, artifact_id=crawl_ref[:64],
                snapshot_hash=snap["snapshot_hash"],
                question_count=snap["question_count"],
                snapshot=snap, created_at=now,
            ))
        else:
            version.snapshot_hash = snap["snapshot_hash"]
            version.question_count = snap["question_count"]
            version.snapshot = snap

    return {"questions_upserted": upserted, "question_count": len(questions),
            "snapshot_hash": snap["snapshot_hash"],
            # M2.3 — what this fold RETIRED, and how much of the catalogue is now
            # history. Reported so a crawl completion can say it out loud instead
            # of a client discovering it from a diff three releases later.
            "questions_retired": retired_now,
            "retired_total": len(all_questions) - len(questions),
            "catalog_total": len(all_questions)}


def _parse_ts(value: Any) -> Any:
    """An ISO timestamp from the pure catalogue back into a datetime, or None.

    The pure layer works in strings because it is DB-free; the column is
    timestamptz. A value that will not parse yields None rather than an
    exception: a malformed timestamp must cost the fold a retirement DATE, never
    the fold.
    """
    from datetime import datetime
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def load_retired_questions(
    tenant_id: str, app_id: str, limit: int = 500,
) -> list[dict[str, Any]]:
    """Every question this application has STOPPED asking — the audit record.

    Read straight off ``catalog_questions``, not reconstructed from the graph, so
    it answers the question an auditor actually asks: what did we catalogue, when
    did we stop seeing it, and on which crawl's evidence. The row keeps its id,
    its content, its pages and its first-seen record; retirement adds to it and
    removes nothing.

    Newest retirement first — a reader looking for "what changed in this release"
    finds it at the top.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant_id,
                CatalogQuestionRow.app_id == app_id,
                CatalogQuestionRow.retired_at.isnot(None),
            ).order_by(CatalogQuestionRow.retired_at.desc()).limit(limit)
        )).scalars().all()
    return [_retired_view(r) for r in rows]


def _retired_view(row: Any) -> dict[str, Any]:
    """One retired question as an audit record — content AND provenance."""
    return {
        "question_id": row.question_id,
        "name": row.name,
        "answer_type": row.answer_type,
        "required": bool(row.required),
        "options": list(row.options or []),
        "validation": row.validation,
        "business_rule": row.business_rule,
        "semantic_type": row.semantic_type,
        "pages": list(row.pages or []),
        "lifecycle": LIFECYCLE_RETIRED,
        "stale": True,
        "retired_at": row.retired_at.isoformat() if row.retired_at else "",
        "retired_in_crawl": row.retired_in_crawl or "",
        "retire_reason": row.retire_reason or "",
        "missed_crawls": int(row.missed_crawls or 0),
        # WHEN IT WAS LAST ACTUALLY SEEN, and in which crawl. ``last_seen_crawl``
        # is the M2.3 column that only ever moves on a real sighting;
        # ``last_seen_artifact`` is the older one that used to bump on every fold
        # and is reported beside it rather than silently replaced.
        "last_seen_crawl": row.last_seen_crawl or "",
        "last_seen_artifact": row.last_seen_artifact or "",
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else "",
        "first_seen_crawl": row.first_seen_artifact or "",
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else "",
    }


async def diff_latest_versions(
    tenant_id: str, app_id: str, limit_older: int = 2
) -> dict[str, Any]:
    """Diff the two most recent catalog versions (P6 regression).

    Returns ``{from, to, diff}`` — ``diff`` from ``catalog_diff.diff_catalogs``.
    With fewer than two versions there is nothing to diff yet.
    """
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CatalogVersionRow).where(
                CatalogVersionRow.tenant_id == tenant_id,
                CatalogVersionRow.app_id == app_id,
            ).order_by(CatalogVersionRow.created_at.desc()).limit(limit_older)
        )).scalars().all()
    if len(rows) < 2:
        return {"from": None, "to": None, "diff": None,
                "reason": "need at least two catalog versions to diff"}
    newer, older = rows[0], rows[1]
    diff = diff_catalogs(older.snapshot or {}, newer.snapshot or {})
    # A BARE LIST OF IDS IS NOT A REPORT. ``removed`` names hashes; a reviewer
    # asked to sign off on "the application stopped asking these" needs the
    # question TEXT and the retirement record behind it. Sourced from the durable
    # rows — the audit record — and falling back to the older snapshot's own copy
    # for a question whose row predates M2.3 and therefore never got stamped.
    diff["removed_detail"] = await _describe_removed(
        tenant_id, app_id, diff.get("removed") or [], older.snapshot or {})
    return {
        # ``crawl_ref`` is what the column actually holds (an exploration id).
        # It was reported as ``artifact_id``, so a consumer joining it against
        # canonical_artifacts would match nothing and quietly show an empty diff.
        # ``artifact_id`` is kept alongside for one release so existing readers
        # do not break on the rename.
        "from": {"crawl_ref": older.artifact_id, "artifact_id": older.artifact_id,
                 "hash": older.snapshot_hash,
                 "question_count": older.question_count},
        "to": {"crawl_ref": newer.artifact_id, "artifact_id": newer.artifact_id,
               "hash": newer.snapshot_hash,
               "question_count": newer.question_count},
        "diff": diff,
    }


async def _describe_removed(
    tenant_id: str, app_id: str, removed_ids: Any, old_snapshot: Any,
) -> list[dict[str, Any]]:
    """The retired questions behind ``diff.removed``, in the diff's own order."""
    ids = [str(q) for q in (removed_ids or []) if str(q)]
    if not ids:
        return []
    async with tenant_scoped_qec_session(tenant_id) as session:
        rows = (await session.execute(
            select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant_id,
                CatalogQuestionRow.app_id == app_id,
                CatalogQuestionRow.question_id.in_(ids),
            ))).scalars().all()
    by_id = {r.question_id: r for r in rows}
    old_by_id = {}
    for q in ((old_snapshot or {}).get("questions") or []):
        if isinstance(q, dict) and q.get("question_id"):
            old_by_id[str(q["question_id"])] = q

    out: list[dict[str, Any]] = []
    for qid in ids:
        row = by_id.get(qid)
        if row is not None:
            out.append(_retired_view(row))
            continue
        # No durable row: report what the previous snapshot held and say plainly
        # that the retirement record is missing, rather than inventing one.
        prev = old_by_id.get(qid) or {}
        out.append({
            "question_id": qid,
            "name": str(prev.get("name") or ""),
            "answer_type": str(prev.get("answer_type") or prev.get("type") or ""),
            "options": list(prev.get("options") or []),
            "pages": list(prev.get("pages") or []),
            "lifecycle": LIFECYCLE_RETIRED,
            "retired_at": "",
            "retired_in_crawl": "",
            "retire_reason": "",
            "record": "snapshot_only",
        })
    return out
