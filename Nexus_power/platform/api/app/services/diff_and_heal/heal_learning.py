"""Heal-outcome LEARNING substrate (P7) — turn the proven/rejected heal ledger into a
BOUNDED confidence PRIOR for future heals.

The HealEventRow ledger (nexus_sdk.db.models) is an append-only, hash-chained record of
every heal decision: which control DRIFTED, how it was fixed (``fix_kind``), and — the
signal we learn from — whether the headed re-run actually PROVED green (``verified_green``)
or was refused/escalated (``engine_verdict``). Over time that history tells us which
SHAPES of drift heal reliably and which don't. This module reads that history (NO new
table — it aggregates the existing ledger) and turns it into a small, bounded nudge on the
similo/re-anchor confidence for the SAME shape of drift.

What it is NOT:
  * It is NOT an oracle. A learned boost can only RE-RANK a candidate within a safe band —
    it can never make an unproven heal "green". Persistence is still gated end-to-end by
    the prove-green re-run + the 2x confirm (see evaluate_heal / the auto-heal loop). A
    high prior on a candidate that then re-runs RED saves nothing.
  * It is NOT able to lower a decision below the refuse-floor on its own: the nudge is
    bounded to ``±PRIOR_MAX`` (~0.1) and the caller applies it ONLY to the ranking /
    display confidence, never to the ``min_confidence`` floor or the ambiguity margin that
    ``resolve_reanchor`` already enforced against the RAW name similarity.

DE-IDENTIFIED by construction. The ``pattern_key`` is a hash of the SHAPE of the drift
only — recorded control kind, resolved role, a COARSE score bucket, and the fix kind. It
carries NO tenant data, NO raw locators, NO accessible names, NO page paths, NO values.
This is deliberate: the de-identified key + a k-anonymous aggregate is EXACTLY the unit
that can later cross tenants (a heal pattern proven at tenant A raises confidence
everywhere) without leaking anyone's app. We build PER-TENANT now (the ``prior`` read is
tenant-scoped) and mark the federation seam (see ``FEDERATION_SEAM`` below).

k-ANONYMITY gate. ``prior`` returns 0.0 (neutral — no nudge) until at least ``K_ANON``
observations exist for the key in the tenant. A single sample can never move a decision,
so the substrate is inert on a cold ledger and only ever speaks once a pattern is
established. Combined with the bound, a noisy early signal cannot shift a heal.

Smoothing. The confirmed-rate is a lower Wilson confidence bound (with a Laplace fallback)
so a small-but-passing sample is treated CONSERVATIVELY: we nudge UP only when the
evidence is strong, and the ``-0.5`` re-centering means a key with a poor confirmed-rate
nudges DOWN (so a shape that historically REFUSES/fails is made HARDER to auto-pick, not
easier).

Storage. NONE added. ``prior`` aggregates ``HealEventRow`` rows for the tenant filtered to
the key's fix_kind + the key's shape; ``record_outcome`` is a no-op pass-through because
the ledger row written by ``heal_evidence.record_heal_event`` IS the record — we derive the
aggregate purely from it. (A future materialized counter table is an optimization, not a
correctness requirement — the seam is noted.)

GATING. On by default is safe: the nudge is bounded, k-anon-gated, and oracle-gated
downstream. An env kill-switch ``NEXUS_HEAL_LEARNING`` (default ON) lets an operator
disable it instantly without a deploy; setting it to a falsy value makes ``prior`` return
0.0 unconditionally (byte-identical to no learning).
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── tunables ──────────────────────────────────────────────────────────────────
# Maximum absolute nudge applied to a similo/re-anchor confidence. Sized to live
# INSIDE the safe band: it is < resolve_reanchor's ambiguity_margin (0.12) and far
# below the gap between the min_confidence floor (0.62) and a confident match, so a
# learned nudge can re-rank near-equal candidates but can NEVER vault a weak-name
# candidate over the refuse-floor (which is checked against RAW name similarity, not
# the nudged value — see the integration plan).
PRIOR_MAX: float = 0.10

# k-anonymity: minimum number of past observations for a key (in this tenant) before
# the prior is allowed to be non-zero. Below this we return 0.0 (neutral) so a single
# sample — or a handful — never moves a decision. Also the unit that gates a future
# CROSS-TENANT aggregate (a federated prior would require >= K_ANON tenants, never
# >= K_ANON rows from one tenant).
K_ANON: int = 5

# Number of coarse score buckets. Coarse on purpose: a fine-grained score would make
# the key near-unique (re-identifying) and would split the ledger into singletons that
# never reach k-anonymity. 5 buckets over [0,1] (0.0-0.2, ... 0.8-1.0).
SCORE_BUCKETS: int = 5

# Wilson lower-bound z (≈ 90% one-sided). Conservative: a small passing sample yields a
# LOW lower bound, so we only nudge meaningfully up once evidence accumulates.
_WILSON_Z: float = 1.2815515594457912

# Hard cap on rows scanned per prior() read — the ledger is append-only and could grow
# large; the aggregate is stable well before this. Bounds the query cost (fail-open).
_MAX_SCAN: int = 5000

# FEDERATION_SEAM: the (pattern_key, n, confirmed) triple produced here is the EXACT
# de-identified unit a federated aggregator would sum ACROSS tenants. To go federated:
# replace the per-tenant `_aggregate` read below with a read from a cross-tenant
# k-anon-gated rollup keyed by `pattern_key` (k measured in TENANTS, not rows). Nothing
# else in this module — key shape, smoothing, bound — changes. Per-tenant today.


def _env_on() -> bool:
    """Kill-switch read. Default ON (bounded + oracle-gated, so safe on by default).
    Any of {0,false,off,no} (case-insensitive) disables learning instantly."""
    v = os.getenv("NEXUS_HEAL_LEARNING", "").strip().lower()
    if v in {"0", "false", "off", "no", "disabled"}:
        return False
    return True


def score_bucket(score: float | None) -> int:
    """Coarse bucket index in [0, SCORE_BUCKETS-1] for a similo/name-similarity score in
    [0,1]. De-identifying on purpose: collapses a continuous score to one of a few bands
    so the key stays shape-only and rows actually collide into a k-anonymous group."""
    try:
        s = float(score if score is not None else 0.0)
    except (TypeError, ValueError):
        s = 0.0
    s = 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)
    b = int(s * SCORE_BUCKETS)
    return SCORE_BUCKETS - 1 if b >= SCORE_BUCKETS else b


def _norm(s: str | None) -> str:
    return " ".join((s or "").strip().lower().split())


def pattern_key(
    *,
    recorded_kind: str,
    resolved_role: str,
    score_bucket: int | float,
    fix_kind: str,
) -> str:
    """A DE-IDENTIFIED signature for a drift+fix SHAPE.

    Inputs are ONLY structural: the recorded control KIND (text/select/toggle/…), the
    RESOLVED live ARIA role the re-anchor would bind to, a COARSE score bucket, and the
    fix channel. NO tenant id, NO raw locators/labels/values/urls — nothing that could
    identify an app or a customer. Two heals of the same shape (e.g. a `text` recorded
    control re-anchored to a `textbox` at score-bucket 3 via `reanchor`) produce the SAME
    key in EVERY tenant, which is what makes the aggregate federatable later.

    Accepts either a pre-computed bucket index or a raw score (it is re-bucketed
    defensively so a caller passing a float can't smuggle a fine-grained value into the
    key). A sha1 prefix keeps it fixed-length and content-opaque."""
    # Normalize the bucket. A caller may pass either a bucket INDEX (an int in
    # [0, SCORE_BUCKETS)) or a raw [0,1] score; both must yield the same key for the same
    # band, so a non-integral float is always treated as a raw score and re-bucketed.
    if isinstance(score_bucket, float) and not score_bucket.is_integer():
        sb = globals()["score_bucket"](score_bucket)   # raw score → bucket index
    else:
        try:
            sb = int(score_bucket)
        except (TypeError, ValueError):
            sb = 0
        if not (0 <= sb < SCORE_BUCKETS):
            # An out-of-range integer: if it looks like a [0,1] score re-bucket, else clamp.
            sb = (globals()["score_bucket"](float(sb))
                  if 0.0 <= float(sb) <= 1.0 else max(0, min(SCORE_BUCKETS - 1, sb)))
    raw = "\x1f".join((
        "v1",
        _norm(recorded_kind),
        _norm(resolved_role),
        str(sb),
        _norm(fix_kind),
    ))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _wilson_lower(confirmed: int, n: int) -> float:
    """Lower bound of the Wilson score interval for a confirmed/n rate. Conservative:
    a small sample yields a low bound even at a high observed rate, so we don't over-
    trust thin evidence. Laplace ((c+1)/(n+2)) is the natural neutral when n is tiny;
    Wilson supersedes it once n grows. n<=0 → 0.5 (neutral, re-centers to 0 nudge)."""
    if n <= 0:
        return 0.5
    z = _WILSON_Z
    phat = confirmed / n
    denom = 1.0 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    lower = (centre - margin) / denom
    # Blend toward Laplace for very small n so a single confirmed/total isn't degenerate.
    laplace = (confirmed + 1.0) / (n + 2.0)
    if n < K_ANON:  # (defensive; prior() already gates on K_ANON before calling)
        return min(lower, laplace)
    return max(0.0, min(1.0, lower))


def _rate_to_nudge(rate_lower: float) -> float:
    """Map a smoothed confirmed-rate lower-bound in [0,1] to a signed nudge in
    [-PRIOR_MAX, +PRIOR_MAX], re-centred at 0.5 (the neutral confirmed-rate). A
    historically-reliable shape (rate→1) nudges UP by at most PRIOR_MAX; a shape that
    historically REFUSES / fails (rate→0) nudges DOWN by at most PRIOR_MAX. Exactly 0.5
    → 0.0 (no change)."""
    delta = (rate_lower - 0.5) * 2.0  # [-1, 1]
    nudge = delta * PRIOR_MAX
    # clamp for safety against float drift
    return max(-PRIOR_MAX, min(PRIOR_MAX, nudge))


async def _aggregate(
    session: AsyncSession, *, tenant_id: str, key: str,
) -> tuple[int, int]:
    """Aggregate the append-only HealEventRow ledger for (tenant, key) into
    (n, confirmed). Re-derives each historical row's pattern_key from its stored shape
    fields and counts only the rows whose key MATCHES — so we never need a key column on
    the ledger (NO new table / column). ``confirmed`` counts rows that re-ran GREEN
    (verified_green) and were not refused as a real regression.

    BEST-EFFORT / FAIL-OPEN: returns (0, 0) on any error (table absent, import issue,
    DB error) so a learning read can NEVER break a heal. The import of HealEventRow is
    local so this module loads even where the SDK model isn't present at import time."""
    try:
        from nexus_sdk.db.models import HealEventRow  # local: SDK may lag in some envs
    except Exception:
        return 0, 0
    try:
        rows = (await session.execute(
            select(
                HealEventRow.fix_kind,
                HealEventRow.engine_verdict,
                HealEventRow.verified_green,
                HealEventRow.details,
            )
            .where(HealEventRow.tenant_id == tenant_id)
            .order_by(HealEventRow.created_at.desc())
            .limit(_MAX_SCAN)
        )).all()
    except Exception:
        return 0, 0

    n = 0
    confirmed = 0
    for fix_kind, engine_verdict, verified_green, details in rows:
        d = details if isinstance(details, dict) else {}
        # The shape fields the writer stamps into details for learning (see integration
        # plan). Absent on legacy rows → that row simply doesn't match any key, so it is
        # ignored (never miscounted). recorded_kind/resolved_role default to "".
        row_key = pattern_key(
            recorded_kind=str(d.get("recorded_kind", "") or ""),
            resolved_role=str(d.get("resolved_role", "") or ""),
            score_bucket=d.get("score_bucket", 0),
            fix_kind=str(fix_kind or d.get("fix_kind", "") or ""),
        )
        if row_key != key:
            continue
        n += 1
        # CONFIRMED = the heal actually proved green AND was not a refused real-regression.
        # engine_verdict carries the loop's terminal verdict; we count green only.
        if bool(verified_green) and "real_regression" not in (engine_verdict or "").lower():
            confirmed += 1
    return n, confirmed


async def prior(
    session: AsyncSession, *, tenant_id: str, key: str,
) -> float:
    """A small, BOUNDED, k-anon-gated confidence nudge in [-PRIOR_MAX, +PRIOR_MAX] for a
    drift+fix SHAPE, learned from this tenant's past heal outcomes.

    Returns 0.0 (NEUTRAL — no change) when:
      * learning is disabled (env kill-switch), or
      * tenant_id / key is empty, or
      * fewer than K_ANON observations exist for the key in this tenant (k-anonymity —
        a single sample, or a thin one, can NEVER move a decision), or
      * any DB/aggregate error (fail-open).

    Otherwise returns ``_rate_to_nudge(wilson_lower(confirmed, n))`` — UP for a shape that
    historically heals reliably, DOWN for one that historically refuses/fails. The caller
    applies this ONLY to the ranking/display confidence, never to the refuse-floor or the
    prove-green oracle, so it can only ever RE-RANK within the safe band."""
    if not _env_on() or not tenant_id or not key:
        return 0.0
    n, confirmed = await _aggregate(session, tenant_id=tenant_id, key=key)
    if n < K_ANON:  # k-anonymity gate — neutral until the pattern is established
        return 0.0
    nudge = _rate_to_nudge(_wilson_lower(confirmed, n))
    if nudge:
        logger.debug(
            "heal_learning.prior",
            extra={"key": key, "n": n, "confirmed": confirmed, "nudge": round(nudge, 4)},
        )
    return nudge


def apply_prior(confidence: float | None, nudge: float, *,
                floor: float = 0.0, ceil: float = 1.0) -> float:
    """Apply a learned ``nudge`` to a similo/re-anchor confidence, clamped to [floor, ceil]
    and to the bounded band. Pure helper so the integration site is a one-liner and the
    bound is enforced in ONE place. Does NOT touch any refuse-floor — the caller must keep
    its own ``min_confidence`` check against the RAW (un-nudged) similarity (see plan)."""
    try:
        c = float(confidence if confidence is not None else 0.0)
    except (TypeError, ValueError):
        c = 0.0
    n = max(-PRIOR_MAX, min(PRIOR_MAX, float(nudge or 0.0)))
    return max(floor, min(ceil, c + n))


async def record_outcome(
    session: AsyncSession, *, tenant_id: str, key: str, verified_green: bool,
) -> bool:
    """No-op pass-through by design — the learning aggregate is DERIVED purely from the
    existing append-only HealEventRow ledger (``heal_evidence.record_heal_event`` already
    writes the authoritative row, and ``_aggregate`` re-derives the key from its stored
    shape fields). There is intentionally NO separate counter table to keep in sync (a
    second write path would be a tamper surface and a consistency risk against the
    hash-chained ledger).

    Kept as a stable seam: if a materialized per-key counter is added later (an
    optimization for very large ledgers, or the cross-tenant federation rollup), THIS is
    where the increment lands — callers won't change. Returns False today (nothing
    written here); a real return only when a counter store is wired."""
    return False


__all__ = [
    "PRIOR_MAX",
    "K_ANON",
    "SCORE_BUCKETS",
    "score_bucket",
    "pattern_key",
    "prior",
    "apply_prior",
    "record_outcome",
]
