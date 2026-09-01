"""DURABLE BUSINESS-RULE STORE — the read/write side of learning that
accumulates (M1.7 / T-GW-04).

Two operations, both tenant-scoped under RLS:

  * :func:`persist_rules` — called when a crawl completes, with the rules it
    proved.  UPSERT semantics: a rule proved again bumps ``version`` /
    ``times_proven`` rather than inserting a second row, so the store's size
    tracks how much has been LEARNED, not how many times it was re-observed.
  * :func:`fetch_rules` — called at dispatch, to hand an incoming crawl what
    earlier crawls of the same application proved.

WHY UPSERT AND NOT INSERT-IF-ABSENT.  A rule that keeps being re-proved is a rule
whose evidence keeps getting fresher, and the freshness is the thing an operator
needs when a rule goes stale (the app changed and the question is no longer
asked).  Ignoring re-proofs would leave every row stamped with the date it was
first seen and no way to tell a live rule from a fossil.

FAIL-OPEN, DELIBERATELY, AND ONLY HERE.  Both functions swallow their errors and
degrade to "no rules": a store that is unreachable must cost a crawl one repeated
experiment, never the crawl.  That is the opposite of the discipline everywhere
else in this milestone, and the asymmetry is the point — a missing OPTIMISATION
is not a false claim.  A missing piece of EVIDENCE is, and nothing in this module
touches evidence.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import tenant_scoped_qec_session, utc_now
from ..db.models import QEBusinessRuleRow

logger = logging.getLogger(__name__)

#: Most rules handed to one crawl.  A dispatch payload is not a place for an
#: unbounded list, and an application with more than this many proved advance
#: gates has a different problem than rule reuse.
MAX_RULES_PER_DISPATCH = 500

#: Most rules accepted from one completion callback.  Mirrors the explorer-side
#: ledger cap; stated here too because this side must never trust the sender.
MAX_RULES_PER_COMPLETION = 200

#: The highest explorer wire-shape version this reader understands.  A rule
#: written by a NEWER explorer is stored (it is evidence, and discarding it would
#: lose it) but is NOT handed back to a crawl until a reader that understands it
#: ships.  See :func:`fetch_rules`.
SUPPORTED_SCHEMA_VERSION = 1


def _row_id(tenant_id: str, app_id: str, rule_key: str) -> str:
    """A deterministic primary key for the rule identity.

    Derived rather than random so an UPSERT can name its own conflict target
    without a round trip, and so the same rule is the same row in every
    environment — which is what makes a store dump comparable across a restore.
    """
    basis = "%s|%s|%s" % (tenant_id, app_id, rule_key)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:48]


def _clean(rule: Mapping[str, Any]) -> dict | None:
    """Validate + bound ONE incoming rule.  ``None`` when it cannot be trusted.

    The explorer is a semi-trusted sender (it holds the fleet secret, but it also
    runs untrusted application JavaScript in the same container), so every string
    that arrives here is truncated to its column width and every required field
    is checked.  A rule missing its key, its blocked control or its field is not
    a rule — it is a fragment, and storing it would put an un-lookup-able row in
    a table whose whole purpose is lookup.
    """
    if not isinstance(rule, Mapping):
        return None
    key = str(rule.get("key") or "").strip()[:64]
    blocked = str(rule.get("blocked_label") or "").strip()[:120]
    field_label = str(rule.get("field_label") or "").strip()[:120]
    if not (key and blocked and field_label):
        return None
    try:
        schema_version = int(rule.get("schema_version") or 1)
    except (TypeError, ValueError):
        return None
    return {
        "rule_key": key,
        "kind": str(rule.get("kind") or "advance_gate")[:32],
        "url_template": str(rule.get("url_template") or "")[:500],
        "blocked_label": blocked,
        "field_label": field_label,
        "proof": str(rule.get("proof") or "")[:500],
        "schema_version": max(1, schema_version),
    }


async def persist_rules(
    tenant_id: str, app_id: str, rules: Iterable[Mapping[str, Any]], *,
    crawl_id: str = "",
) -> int:
    """Store the rules a completed crawl proved.  Returns how many were written.

    Idempotent: re-delivering the same completion (which T-GW-02 makes a normal,
    expected event) re-upserts the same rows, bumping ``times_proven``.  That
    over-counts a re-proof on a duplicate callback, and the alternative — keying
    the count on crawl_id and carrying a per-crawl dedup — buys precision in a
    counter at the cost of a second table.  The count is a freshness signal, not
    an accounting record, so the cheap version is the right one.
    """
    if not tenant_id or not app_id:
        return 0
    cleaned = []
    seen: set[str] = set()
    for raw in list(rules or ())[:MAX_RULES_PER_COMPLETION]:
        row = _clean(raw)
        if row is None or row["rule_key"] in seen:
            continue
        seen.add(row["rule_key"])
        cleaned.append(row)
    if not cleaned:
        return 0

    now = utc_now()
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            for row in cleaned:
                statement = pg_insert(QEBusinessRuleRow).values(
                    rule_row_id=_row_id(tenant_id, app_id, row["rule_key"]),
                    tenant_id=tenant_id, app_id=app_id,
                    last_crawl_id=str(crawl_id or "")[:50],
                    first_proven_at=now, last_proven_at=now,
                    version=1, times_proven=1,
                    **row,
                )
                # ON CONFLICT on the IDENTITY index, not the primary key: the
                # identity is what "the same rule" means, and keying the conflict
                # on the surrogate id would only work because the surrogate is
                # derived from the identity — a coincidence that would break
                # silently the day the derivation changed.
                await session.execute(statement.on_conflict_do_update(
                    index_elements=["tenant_id", "app_id", "rule_key"],
                    set_={
                        "version": QEBusinessRuleRow.version + 1,
                        "times_proven": QEBusinessRuleRow.times_proven + 1,
                        "last_proven_at": now,
                        "last_crawl_id": str(crawl_id or "")[:50],
                        # The PROOF is refreshed: the newest observation is the
                        # one that describes the application as it is now.
                        "proof": row["proof"],
                        "url_template": row["url_template"],
                        "field_label": row["field_label"],
                        "schema_version": row["schema_version"],
                    },
                ))
            await session.commit()
    except Exception as exc:
        logger.warning(
            "qec.rules.persist_failed",
            extra={"tenant_id": tenant_id, "app_id": app_id,
                   "crawl_id": crawl_id, "error": str(exc)[:300]},
        )
        return 0
    logger.info(
        "qec.rules.persisted",
        extra={"tenant_id": tenant_id, "app_id": app_id, "crawl_id": crawl_id,
               "rules": len(cleaned)},
    )
    return len(cleaned)


async def fetch_rules(tenant_id: str, app_id: str, *,
                      limit: int = MAX_RULES_PER_DISPATCH) -> list[dict]:
    """Every rule this tenant has proved about this app, newest-proven first.

    The shape returned is exactly the explorer's wire shape
    (:class:`app.rules.DiscoveredRule`), so the dispatch is a pass-through and
    the two sides share one vocabulary rather than a translation nobody owns.

    Rules written by a NEWER explorer than this reader understands are filtered
    out here rather than at the explorer: the explorer's own parser is also
    fail-closed on the version, so filtering twice is redundant — and redundant
    is the correct posture for the boundary between a store and the engine that
    acts on it.
    """
    if not tenant_id or not app_id:
        return []
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            rows = (await session.execute(
                select(QEBusinessRuleRow)
                .where(QEBusinessRuleRow.tenant_id == tenant_id,
                       QEBusinessRuleRow.app_id == app_id,
                       QEBusinessRuleRow.schema_version <= SUPPORTED_SCHEMA_VERSION)
                .order_by(QEBusinessRuleRow.last_proven_at.desc())
                .limit(max(1, int(limit)))
            )).scalars().all()
    except Exception as exc:
        logger.warning(
            "qec.rules.fetch_failed",
            extra={"tenant_id": tenant_id, "app_id": app_id, "error": str(exc)[:300]},
        )
        return []
    return [
        {
            "key": row.rule_key,
            "kind": row.kind,
            "url_template": row.url_template,
            "blocked_label": row.blocked_label,
            "field_label": row.field_label,
            "proof": row.proof,
            "schema_version": row.schema_version,
        }
        for row in rows
    ]


def reuse_metrics(coverage: Any) -> dict:
    """Project a completion's ``coverage.rule_reuse`` onto a flat metric bundle.

    Pure.  Exists so the reuse RATE — a headline metric of this milestone — is
    read from ONE place by the exploration row, the fleet funnel and the tests,
    instead of each re-reaching into the coverage blob with its own key spelling.
    """
    stats = {}
    if isinstance(coverage, Mapping):
        raw = coverage.get("rule_reuse")
        if isinstance(raw, Mapping):
            stats = raw
    def _int(name: str) -> int:
        try:
            return int(stats.get(name) or 0)
        except (TypeError, ValueError):
            return 0
    hits, misses = _int("hits"), _int("misses")
    lookups = hits + misses
    return {
        "rules_known": _int("known"),
        "rule_lookups": lookups,
        "rules_reused": hits,
        "rule_reuse_rate": round(hits / lookups, 4) if lookups else 0.0,
    }


__all__ = [
    "MAX_RULES_PER_DISPATCH", "MAX_RULES_PER_COMPLETION",
    "SUPPORTED_SCHEMA_VERSION", "persist_rules", "fetch_rules", "reuse_metrics",
]
