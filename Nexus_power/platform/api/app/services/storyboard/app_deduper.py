"""App deduplication — fold OCR-corrupted app instances into canonical groupings.

The pipeline detects a new ``AppInstanceRow`` whenever window-title OCR or
domain extraction changes between consecutive scenes.  In practice this
produces fakes:

* ``"Wivwquardianlife.com"`` — OCR error: V/W confusion + missing dot
* ``"wwwguardianlife.com"`` — missing dot between www and guardianlife
* ``"guardianlife.com"`` — the correct one
* ``"guardianlife.com  "`` — trailing space from a different scan

A 2-hour SME demo on one website easily produces 5-7 fake "app
instances" the storyboard then shows as separate apps.  This service
folds those into one canonical ``AppGroupingRow`` per registered
domain (or normalised window title for desktop apps).

The merge is tiered:

1. **Exact normalised domain** — strip ``www``, lowercase, trim whitespace.
   This catches the most common case.
2. **Fuzzy domain match** — Levenshtein distance ≤ ``domain_levenshtein_max``
   AND character overlap ratio ≥ ``domain_min_overlap_ratio``.  Catches
   OCR errors.
3. **Normalised window title** (when enabled) — for desktop apps where
   there is no URL we can compare.  Strips trailing app version strings
   ("- Microsoft Excel 365 Pro" → "microsoft excel").
4. **Single-instance** — when no merge is possible the instance gets
   its own grouping with ``dedup_basis = single_instance``.

Implementation notes:

* No new pip dependency is REQUIRED.  ``tldextract`` and ``rapidfuzz``
  are auto-detected and used when present (faster, more accurate);
  otherwise the service falls back to a built-in registered-domain
  extractor and a hand-rolled Levenshtein.  Production deployments
  should install both via ``platform/api/requirements.txt``.
* Idempotent: deterministic ``grouping_id`` via uuid5 keeps re-runs
  stable.
* After upserting the groupings the service back-links
  ``storyboard_panels.app_grouping_id`` so the storyboard composer
  does not have to join through ``app_instances`` at render time.

Consumed by ``composer.run_for_artifact``.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_sdk.db.models import (
    AppGroupingRow,
    AppInstanceRow,
    StoryboardPanelRow,
    VisualSceneRow,
)

from .config import AppDeduperConfig


logger = logging.getLogger(__name__)


_NAMESPACE_STORYBOARD = uuid.UUID("d4f6c9a2-6d8b-4f5b-9a32-8f4b1c1a2d3e")
"""Same namespace as scene_grouper — keeps storyboard derived IDs grouped."""


# ── Optional fast libraries (auto-detected) ───────────────────────────────────

try:  # pragma: no cover - import guarded by capability check at runtime
    import tldextract  # type: ignore[import-untyped]

    _TLD_EXTRACT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TLD_EXTRACT_AVAILABLE = False

try:  # pragma: no cover
    from rapidfuzz.distance import Levenshtein as _rf_levenshtein  # type: ignore[import-untyped]

    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RAPIDFUZZ_AVAILABLE = False


# ── Domain extraction ─────────────────────────────────────────────────────────

# A small but practical multi-label TLD list used when ``tldextract`` is not
# installed.  Covers ~99% of real-world domains for English-speaking enterprise
# QA workflows.  Not a substitute for the full Public Suffix List — production
# deployments should install ``tldextract`` for proper coverage.
_MULTI_LABEL_TLDS = frozenset({
    "co.uk", "ac.uk", "gov.uk", "org.uk", "ltd.uk",
    "co.in", "ac.in", "gov.in", "co.jp", "ac.jp",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "ac.nz", "co.za", "gov.za",
    "com.br", "com.mx", "com.cn", "com.sg", "com.hk",
})

_DOMAIN_LIKE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+"
)

_NORMALISE_DOMAIN_RE = re.compile(r"^(https?://)?(www\.)?", re.IGNORECASE)
_TRAILING_PATH_RE = re.compile(r"[/?#].*$")
_NON_WORD_RE = re.compile(r"[^A-Za-z0-9]+")


def _strip_protocol(value: str) -> str:
    """Trim ``http(s)://`` + leading ``www.`` so comparison is apples-to-apples."""
    if not value:
        return ""
    value = value.strip()
    value = _NORMALISE_DOMAIN_RE.sub("", value)
    value = _TRAILING_PATH_RE.sub("", value)
    return value.lower()


def _extract_registered_domain(value: str) -> str:
    """Return the registered domain (eTLD+1) for a URL or hostname.

    Uses ``tldextract`` when available; otherwise falls back to the
    built-in last-two-labels heuristic with a small multi-label TLD
    table.  Returns ``""`` when the input has no recognisable domain
    (desktop apps, terminal screens, blank scenes).
    """
    cleaned = _strip_protocol(value)
    if not cleaned:
        return ""

    if _TLD_EXTRACT_AVAILABLE:
        try:
            extracted = tldextract.extract(cleaned)
            if extracted.domain and extracted.suffix:
                return f"{extracted.domain}.{extracted.suffix}".lower()
            if extracted.domain:
                return extracted.domain.lower()
        except Exception:  # pragma: no cover - tldextract failures fall through
            pass

    # Last-two-labels fallback with multi-label TLD awareness.
    labels = [lbl for lbl in cleaned.split(".") if lbl]
    if len(labels) < 2:
        return cleaned

    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_TLDS and len(labels) >= 3:
        return ".".join(labels[-3:]).lower()
    return last_two.lower()


def _candidate_domain_from_instance(instance: AppInstanceRow) -> str:
    """Pull a candidate registered domain from a row.

    The pipeline writes a URL into ``detected_url`` when it can; otherwise
    it falls back to ``window_title`` which often contains domain-like
    text after OCR.  We try both, preferring the URL when present.
    """
    url_domain = _extract_registered_domain(instance.detected_url or "")
    if url_domain:
        return url_domain
    # Window title sometimes contains a URL like "Guardian Life — guardianlife.com"
    match = _DOMAIN_LIKE.search(instance.window_title or "")
    if match:
        return _extract_registered_domain(match.group(0))
    return ""


_APP_SUFFIX_NOISE = re.compile(
    r"\b("
    r"google chrome|microsoft edge|firefox|safari|opera|brave|"
    r"microsoft (?:excel|word|powerpoint|outlook|teams)|"
    r"chrome|edge|"
    r"\d+\.\d+(?:\.\d+)?"
    r")\b",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
_DASH_RUN_RE = re.compile(r"[–—―\-]+\s*$")


def _normalise_window_title(title: str) -> str:
    """Strip common browser/app suffixes so window titles compare cleanly.

    "Guardian Life — Microsoft Edge"  → "guardian life"
    "guardian life - Google Chrome"   → "guardian life"
    "Excel - Workbook1.xlsx"          → "workbook1.xlsx"
    """
    if not title:
        return ""
    cleaned = title.strip().lower()
    cleaned = _APP_SUFFIX_NOISE.sub(" ", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _DASH_RUN_RE.sub("", cleaned)
    return cleaned.strip(" -—")


# ── Distance metrics (stdlib fallback when rapidfuzz absent) ──────────────────


def _levenshtein(a: str, b: str) -> int:
    """Pure-Python Levenshtein distance.

    Used only when ``rapidfuzz`` is unavailable.  Domain strings are
    typically 5-40 chars so the O(NM) cost is trivial.
    """
    if _RAPIDFUZZ_AVAILABLE:
        return int(_rf_levenshtein.distance(a, b))

    if not a:
        return len(b)
    if not b:
        return len(a)
    if a == b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    previous_row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current_row = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            insert = previous_row[j] + 1
            delete_ = current_row[j - 1] + 1
            substitute = previous_row[j - 1] + (ca != cb)
            current_row[j] = min(insert, delete_, substitute)
        previous_row = current_row
    return previous_row[-1]


def _char_overlap_ratio(a: str, b: str) -> float:
    """Bigram overlap ratio — robust to typos that swap or repeat characters.

    Defined as ``|A_bigrams ∩ B_bigrams| / |A_bigrams ∪ B_bigrams|``.
    Returns 1.0 for identical inputs, 0.0 for fully disjoint, never raises.
    """
    a_clean = _NON_WORD_RE.sub("", a or "").lower()
    b_clean = _NON_WORD_RE.sub("", b or "").lower()
    if not a_clean or not b_clean:
        return 0.0
    if a_clean == b_clean:
        return 1.0

    def bigrams(s: str) -> set[str]:
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}

    bi_a = bigrams(a_clean)
    bi_b = bigrams(b_clean)
    union = bi_a | bi_b
    if not union:
        return 0.0
    return len(bi_a & bi_b) / len(union)


# ── Clustering ────────────────────────────────────────────────────────────────


@dataclass
class _InstancePayload:
    """Internal view of one ``AppInstanceRow`` after preprocessing."""

    instance: AppInstanceRow
    candidate_domain: str
    normalised_title: str

    # Used by the second-pass fuzzy clustering loop.
    cluster_index: int | None = None


@dataclass
class _Cluster:
    """One canonical app grouping under construction."""

    members: list[_InstancePayload] = field(default_factory=list)
    canonical_domain: str = ""
    representative_app_name: str = ""
    representative_app_type: str = "unknown"
    dedup_basis: str = "single_instance"
    dedup_evidence: dict = field(default_factory=dict)
    base_confidence: float = 1.0


def _is_fuzzy_domain_match(
    candidate: str,
    existing: str,
    *,
    config: AppDeduperConfig,
) -> tuple[bool, dict]:
    """Decide whether two domain strings are likely the same after OCR.

    Returns ``(match, evidence_dict)``.  When True the caller should
    place the candidate into the same cluster as ``existing``.
    """
    if not candidate or not existing:
        return False, {}
    if candidate == existing:
        return True, {"basis": "exact_match", "distance": 0, "overlap": 1.0}

    distance = _levenshtein(candidate, existing)
    if distance > config.domain_levenshtein_max:
        return False, {
            "basis": "levenshtein_too_far",
            "distance": distance,
            "threshold": config.domain_levenshtein_max,
        }

    overlap = _char_overlap_ratio(candidate, existing)
    if overlap < config.domain_min_overlap_ratio:
        return False, {
            "basis": "overlap_too_low",
            "distance": distance,
            "overlap": overlap,
            "threshold": config.domain_min_overlap_ratio,
        }

    return True, {
        "basis": "fuzzy_domain",
        "distance": distance,
        "overlap": round(overlap, 3),
    }


def _cluster_instances(
    payloads: Sequence[_InstancePayload],
    *,
    config: AppDeduperConfig,
) -> list[_Cluster]:
    """Tiered clustering pass.

    1. Exact-domain bucketing (O(N))
    2. Fuzzy-domain absorption against existing clusters (O(N*K))
    3. Window-title fallback for desktop apps
    4. Single-instance leftovers
    """
    clusters: list[_Cluster] = []

    # Tier 1 — exact normalised domain → fast dict lookup.
    by_exact_domain: dict[str, _Cluster] = {}
    no_domain_payloads: list[_InstancePayload] = []
    for payload in payloads:
        domain = payload.candidate_domain
        if not domain:
            no_domain_payloads.append(payload)
            continue
        cluster = by_exact_domain.get(domain)
        if cluster is None:
            cluster = _Cluster(
                canonical_domain=domain,
                representative_app_name=domain,
                representative_app_type=payload.instance.app_type or "unknown",
                dedup_basis="exact_domain",
                dedup_evidence={"primary_strings": [domain]},
                base_confidence=float(payload.instance.segmentation_confidence or 1.0),
            )
            by_exact_domain[domain] = cluster
            clusters.append(cluster)
        cluster.members.append(payload)
        cluster.base_confidence = min(
            cluster.base_confidence,
            float(payload.instance.segmentation_confidence or 1.0),
        )

    # Tier 2 — fuzzy absorption between buckets formed in tier 1.  We
    # consolidate similar buckets greedily: each unmerged bucket is
    # compared against every later bucket; on first match we absorb the
    # smaller into the larger.
    if clusters:
        i = 0
        while i < len(clusters):
            host = clusters[i]
            j = i + 1
            while j < len(clusters):
                other = clusters[j]
                match, evidence = _is_fuzzy_domain_match(
                    host.canonical_domain,
                    other.canonical_domain,
                    config=config,
                )
                if match:
                    # Absorb ``other`` into ``host``.  Keep the longer
                    # canonical_domain (assume it has more correct
                    # characters) and union the evidence.
                    if len(other.canonical_domain) > len(host.canonical_domain):
                        host.canonical_domain = other.canonical_domain
                        host.representative_app_name = (
                            other.representative_app_name or host.representative_app_name
                        )
                    host.members.extend(other.members)
                    host.dedup_basis = "fuzzy_domain"
                    fuzzy = host.dedup_evidence.setdefault("fuzzy_merges", [])
                    fuzzy.append(evidence | {"merged_string": other.canonical_domain})
                    host.dedup_evidence.setdefault("primary_strings", []).append(
                        other.canonical_domain,
                    )
                    host.base_confidence = min(host.base_confidence, other.base_confidence)
                    clusters.pop(j)
                    continue
                j += 1
            i += 1

    # Tier 3 — window-title grouping for domain-less payloads (desktop apps).
    if no_domain_payloads and config.allow_window_title_grouping:
        by_title: dict[str, _Cluster] = {}
        for payload in no_domain_payloads:
            title = payload.normalised_title
            if not title:
                # No domain AND no usable title — defer to tier 4.
                continue
            existing = by_title.get(title)
            if existing is None:
                existing = _Cluster(
                    canonical_domain="",
                    representative_app_name=(
                        payload.instance.app_name or payload.instance.window_title or "Unknown App"
                    ),
                    representative_app_type=payload.instance.app_type or "desktop",
                    dedup_basis="window_title_normalized",
                    dedup_evidence={"normalised_title": title},
                    base_confidence=float(payload.instance.segmentation_confidence or 1.0),
                )
                by_title[title] = existing
                clusters.append(existing)
            existing.members.append(payload)
            existing.base_confidence = min(
                existing.base_confidence,
                float(payload.instance.segmentation_confidence or 1.0),
            )

    # Tier 4 — singletons (no domain, no title).  Each gets its own cluster.
    for payload in no_domain_payloads:
        if not payload.normalised_title or not config.allow_window_title_grouping:
            clusters.append(
                _Cluster(
                    canonical_domain="",
                    representative_app_name=(
                        payload.instance.app_name or payload.instance.window_title or "Unknown App"
                    ),
                    representative_app_type=payload.instance.app_type or "unknown",
                    dedup_basis="single_instance",
                    dedup_evidence={},
                    base_confidence=float(payload.instance.segmentation_confidence or 1.0),
                    members=[payload],
                )
            )

    return clusters


@dataclass
class GroupingCandidate:
    """Result of clustering — turned into an ``AppGroupingRow`` by the persister."""

    grouping_id: str
    canonical_name: str
    canonical_domain: str
    app_type: str
    display_label: str
    member_instance_ids: list[str]
    total_scene_count: int
    first_scene_index: int
    last_scene_index: int
    confidence: float
    dedup_basis: str
    dedup_evidence: dict


def _grouping_id(artifact_id: str, canonical_key: str, fallback_index: int) -> str:
    """Deterministic grouping_id — domain wins when present, else stable index."""
    seed = canonical_key.strip().lower() if canonical_key else f"singleton-{fallback_index}"
    return str(uuid.uuid5(
        _NAMESPACE_STORYBOARD,
        f"app_grouping:{artifact_id}:{seed}",
    ))


def _clusters_to_candidates(
    artifact_id: str, clusters: Sequence[_Cluster],
) -> list[GroupingCandidate]:
    """Convert internal clusters into persistable ``GroupingCandidate`` rows."""
    candidates: list[GroupingCandidate] = []
    for idx, cluster in enumerate(clusters):
        if not cluster.members:
            continue
        member_ids = [m.instance.instance_id for m in cluster.members]
        scene_total = sum(int(m.instance.scene_count or 0) for m in cluster.members)
        first_index = min(int(m.instance.first_scene_index or 0) for m in cluster.members)
        last_index = max(int(m.instance.last_scene_index or 0) for m in cluster.members)

        # Confidence: combine the worst original segmentation_confidence
        # with a clustering penalty when fuzzy merges occurred.
        confidence = cluster.base_confidence
        if cluster.dedup_basis == "fuzzy_domain":
            confidence = min(confidence, 0.85)
        elif cluster.dedup_basis == "window_title_normalized":
            confidence = min(confidence, 0.75)
        elif cluster.dedup_basis == "single_instance":
            confidence = min(confidence, 1.0)

        # Display label — prefer the longest non-empty app_name across
        # members, falling back to the canonical domain.
        names = sorted(
            {m.instance.app_name for m in cluster.members if m.instance.app_name},
            key=len,
            reverse=True,
        )
        display_label = (
            names[0]
            if names
            else (cluster.canonical_domain or cluster.representative_app_name or "Unknown App")
        )
        canonical_name = display_label

        candidates.append(GroupingCandidate(
            grouping_id=_grouping_id(
                artifact_id,
                cluster.canonical_domain or cluster.representative_app_name,
                idx,
            ),
            canonical_name=canonical_name,
            canonical_domain=cluster.canonical_domain,
            app_type=cluster.representative_app_type,
            display_label=display_label,
            member_instance_ids=member_ids,
            total_scene_count=scene_total,
            first_scene_index=first_index,
            last_scene_index=last_index,
            confidence=round(confidence, 4),
            dedup_basis=cluster.dedup_basis,
            dedup_evidence=cluster.dedup_evidence,
        ))

    candidates.sort(key=lambda c: c.first_scene_index)
    return candidates


async def _load_instances(
    session: AsyncSession, *, artifact_id: str, tenant_id: str,
) -> list[AppInstanceRow]:
    stmt = (
        select(AppInstanceRow)
        .where(
            AppInstanceRow.artifact_id == artifact_id,
            AppInstanceRow.tenant_id == tenant_id,
        )
        .order_by(AppInstanceRow.first_scene_index.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _upsert_groupings(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    candidates: Sequence[GroupingCandidate],
    config: AppDeduperConfig,
) -> int:
    if not candidates:
        return 0
    rows = [
        {
            "grouping_id": c.grouping_id,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "canonical_name": c.canonical_name,
            "canonical_domain": c.canonical_domain,
            "app_type": c.app_type,
            "display_label": c.display_label,
            "member_instance_ids": c.member_instance_ids,
            "total_scene_count": c.total_scene_count,
            "first_scene_index": c.first_scene_index,
            "last_scene_index": c.last_scene_index,
            "confidence": c.confidence,
            "dedup_basis": c.dedup_basis,
            "dedup_evidence": c.dedup_evidence,
            "deduper_version": config.version,
        }
        for c in candidates
    ]
    stmt = pg_insert(AppGroupingRow.__table__).values(rows)
    update_columns = {
        col.name: stmt.excluded[col.name]
        for col in AppGroupingRow.__table__.columns
        if col.name not in {"grouping_id", "created_at"}
    }
    update_columns["updated_at"] = stmt.excluded.updated_at
    upsert = stmt.on_conflict_do_update(
        index_elements=[AppGroupingRow.__table__.c.grouping_id],
        set_=update_columns,
    )
    await session.execute(upsert)
    return len(rows)


async def _delete_orphan_groupings(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    keep_ids: set[str],
) -> int:
    stmt = delete(AppGroupingRow).where(
        AppGroupingRow.artifact_id == artifact_id,
        AppGroupingRow.tenant_id == tenant_id,
    )
    if keep_ids:
        stmt = stmt.where(~AppGroupingRow.grouping_id.in_(keep_ids))
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def _backlink_panels_to_groupings(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    candidates: Sequence[GroupingCandidate],
) -> int:
    """Populate ``storyboard_panels.app_grouping_id`` based on member instances.

    Walks each grouping's member ``AppInstanceRow`` ids → finds the
    scenes that belong to those instances → updates panels whose scene
    range intersects.  Uses a single UPDATE per grouping so the
    operation stays O(G) regardless of panel count.
    """
    touched = 0
    for candidate in candidates:
        if not candidate.member_instance_ids:
            continue
        # Find scenes in this grouping
        scene_q = (
            select(VisualSceneRow.scene_id)
            .where(
                VisualSceneRow.artifact_id == artifact_id,
                VisualSceneRow.tenant_id == tenant_id,
                VisualSceneRow.app_instance_id.in_(candidate.member_instance_ids),
            )
        )
        scene_result = await session.execute(scene_q)
        scene_ids = [row[0] for row in scene_result.all()]
        if not scene_ids:
            continue

        # Update every panel whose first_scene_id is one of these scenes.
        # That keeps the back-link simple and matches one row per panel
        # — first_scene_id is unique within an artifact in practice.
        update_stmt = (
            update(StoryboardPanelRow)
            .where(
                StoryboardPanelRow.artifact_id == artifact_id,
                StoryboardPanelRow.tenant_id == tenant_id,
                StoryboardPanelRow.first_scene_id.in_(scene_ids),
            )
            .values(app_grouping_id=candidate.grouping_id, updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        )
        result = await session.execute(update_stmt)
        touched += int(result.rowcount or 0)
    return touched


@dataclass(frozen=True)
class DeduperResult:
    """Summary returned to the composer."""

    artifact_id: str
    groupings_written: int
    orphans_removed: int
    panels_relinked: int
    raw_instance_count: int
    elapsed_ms: int


async def rededuplicate_artifact(
    session: AsyncSession,
    *,
    artifact_id: str,
    tenant_id: str,
    session_id: str,
    config: AppDeduperConfig,
) -> DeduperResult:
    """Re-derive app groupings for one artifact.

    Idempotent and safe to call after every pipeline run.  Caller must
    have set the tenant RLS context on the session.  Does not commit.
    """
    started_at = time.monotonic()

    instances = await _load_instances(
        session, artifact_id=artifact_id, tenant_id=tenant_id,
    )
    payloads: list[_InstancePayload] = []
    for instance in instances:
        domain = _candidate_domain_from_instance(instance)
        title = _normalise_window_title(instance.window_title or "")
        payloads.append(_InstancePayload(
            instance=instance,
            candidate_domain=domain,
            normalised_title=title,
        ))

    clusters = _cluster_instances(payloads, config=config)
    candidates = _clusters_to_candidates(artifact_id, clusters)

    written = await _upsert_groupings(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        session_id=session_id,
        candidates=candidates,
        config=config,
    )
    keep_ids = {c.grouping_id for c in candidates}
    orphans = await _delete_orphan_groupings(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        keep_ids=keep_ids,
    )
    relinked = await _backlink_panels_to_groupings(
        session,
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        candidates=candidates,
    )

    elapsed_ms = int((time.monotonic() - started_at) * 1000)

    logger.info(
        "storyboard.app_deduper.rededuplicate",
        extra={
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "raw_instance_count": len(instances),
            "groupings_written": written,
            "orphans_removed": orphans,
            "panels_relinked": relinked,
            "elapsed_ms": elapsed_ms,
            "deduper_version": config.version,
            "tldextract_available": _TLD_EXTRACT_AVAILABLE,
            "rapidfuzz_available": _RAPIDFUZZ_AVAILABLE,
        },
    )

    return DeduperResult(
        artifact_id=artifact_id,
        groupings_written=written,
        orphans_removed=orphans,
        panels_relinked=relinked,
        raw_instance_count=len(instances),
        elapsed_ms=elapsed_ms,
    )


def cluster_payloads(
    payloads: Sequence[_InstancePayload],
    *,
    artifact_id: str,
    config: AppDeduperConfig,
) -> list[GroupingCandidate]:
    """Pure helper exposed for unit tests.

    Allows tests to exercise the clustering logic without a database
    by passing hand-built ``_InstancePayload`` instances.
    """
    return _clusters_to_candidates(
        artifact_id,
        _cluster_instances(list(payloads), config=config),
    )


# Re-exported for tests that need to construct payloads by hand.
__all__ = [
    "DeduperResult",
    "GroupingCandidate",
    "_InstancePayload",
    "_candidate_domain_from_instance",
    "_extract_registered_domain",
    "_normalise_window_title",
    "cluster_payloads",
    "rededuplicate_artifact",
]
