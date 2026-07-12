"""QE-Central — THE atomic substrate writer (design §2.1-§2.3).

Maps a validated :class:`ExplorationBundle` onto the VKPower substrate the
UNCHANGED factory chain reads, inside ONE tenant-scoped transaction on the
nexus DB (role ``qec_substrate``):

  write order:  tenant/artifact bootstrap-check → page_visits →
                page_actions → ground_truth_events (audit journal) →
                visual_frames

Hard rules enforced here (§2.3 — make-or-break):
  1. ONE ``extractor_version`` string per write, stamped on every row —
     the factory's latest-wins selection filters on it
     (test_factory/service.py:44-58);
  2. UNIQUE-constraint-safe: an already-written version is REFUSED before
     any insert (``uq_page_visits_artifact_sequence_version`` /
     ``uq_page_actions_visit_version_subaction`` can then never fire
     mid-transaction), so a re-crawl always arrives as a NEW version;
  3. atomicity: any failure rolls the whole transaction back — zero rows
     of a half-written version can ever exist (screenshot uploads happen
     BEFORE the transaction; an orphaned PNG is harmless, a dangling
     ``visual_frames`` row is not);
  4. PII is redacted AT SOURCE via :mod:`.redact` — form values, action
     values and journal values are never persisted raw.

Field values are pinned to the verified factory contract (§2.1 table):
``source='ground_truth'`` (generator PROVEN gate, generator.py:556),
``extraction_confidence=1.0``, ``confidence=1.0``, ``automation_ready=True``,
``evidence_signals={anchor, after, url_changed, qec}`` (unpacked verbatim by
service.py:152-176).
"""
from __future__ import annotations

import logging
import uuid

from pydantic import BaseModel
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexus_sdk.db.models import (
    CanonicalArtifactRow,
    GroundTruthEventRow,
    PageActionRow,
    PageVisitRow,
    TenantRow,
    VisualFrameRow,
)

from ..db import new_id, tenant_scoped_substrate_session, utc_now
from .assets import FrameRef, store_screenshots
from .redact import RedactionResult, merge_counts, redact_secret, redact_value
from .schema import ActionRecord, ExplorationBundle, PageState, RefusalError, validate_bundle

logger = logging.getLogger(__name__)

# page_visits.extractor_version / page_actions.extractor_version are
# String(50) (alembic 034:135 / 035:86) — enforce the ceiling honestly.
EXTRACTOR_VERSION_MAX = 50
#: The pinned writer-generation prefix (design R-4).
EXTRACTOR_VERSION_PREFIX = "qec_live_v1@"

#: page_visits.source — the EXISTING trusted value, deliberately (R-2):
#: generator ``_PROVEN_SOURCES`` gates hard toHaveURL on it.
VISIT_SOURCE = "ground_truth"
#: ground_truth_events provenance (§2.1 journal row).
GT_MODALITY = "qe_explorer"
GT_RECORDER_VERSION = "qec_explorer_v1"

_ACTION_VALUE_MAX = 1000  # ground_truth_events.value is String(1000)


class WriteStats(BaseModel):
    """Counts for one atomic substrate write (returned to the router and
    persisted on the ``qe_explorations`` row).  ``redactions`` is the total
    number of PII replacements applied at source; ``unredacted_errors``
    counts detector fail-open passes (visible, never silent)."""

    visits: int = 0
    actions: int = 0
    screenshots: int = 0
    frames: int = 0
    events: int = 0
    redactions: int = 0
    unredacted_errors: int = 0


def validate_extractor_version(version: str) -> str:
    """Enforce the §2.3 version-string rules; returns the clean string.

    Raises :class:`RefusalError` (honest, enumerated) on an empty,
    over-long, or wrong-prefix version — a malformed version would either
    truncate in the column or break latest-wins selection.
    """
    v = (version or "").strip()
    if not v:
        raise RefusalError("invalid_extractor_version", "empty version string")
    if len(v) > EXTRACTOR_VERSION_MAX:
        raise RefusalError(
            "invalid_extractor_version",
            f"{v[:60]!r} exceeds {EXTRACTOR_VERSION_MAX} chars "
            f"(page_visits.extractor_version String(50) cap)",
        )
    if not v.startswith(EXTRACTOR_VERSION_PREFIX):
        raise RefusalError(
            "invalid_extractor_version",
            f"{v!r} does not carry the pinned prefix "
            f"{EXTRACTOR_VERSION_PREFIX!r} (design R-4)",
        )
    return v


def _is_password_signal(signals: dict, label: str) -> bool:
    sig = signals.get(label) or {}
    return str(sig.get("type") or "").strip().lower() == "password"


def action_is_secret(action: ActionRecord, page: PageState) -> bool:
    """True when the capture signals flag this action's value as a secret."""
    if str((action.qec or {}).get("input_type") or "").strip().lower() == "password":
        return True
    return _is_password_signal(page.form_snapshot_signals, action.target_label)


def redacted_form_snapshot(page: PageState) -> tuple[dict[str, str], dict[str, int], int]:
    """Redact one visit's form snapshot → (snapshot, class counts, errors)."""
    out: dict[str, str] = {}
    counts: dict[str, int] = {}
    errors = 0
    for label, value in page.form_snapshot.items():
        if _is_password_signal(page.form_snapshot_signals, label):
            result = redact_secret(str(value))
        else:
            result = redact_value(str(value))
        out[str(label)] = result.value
        merge_counts(counts, result)
        errors += result.errors
    return out, counts, errors


def redacted_action_value(action: ActionRecord, page: PageState) -> RedactionResult:
    """Redact one action's committed value (signal-flagged secrets whole)."""
    if action.value is None:
        return RedactionResult(value="")
    if action_is_secret(action, page):
        return redact_secret(str(action.value))
    return redact_value(str(action.value))


def _gt_event_id(artifact_id: str, sequence_index: int, kind: str, timestamp_ms: int) -> str:
    """uuid5-deduplicated journal event id — byte-identical recipe to the
    platform-api GT ingest route (storyboard.py:818-821) so re-writes of
    the same evidence can never double-journal."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"nexus-gt:{artifact_id}:{sequence_index}:{kind}:{timestamp_ms}",
    ))


def _evidence_signals(action: ActionRecord) -> dict:
    """The verbatim ``evidence_signals`` bundle the factory unpacks
    (service.py:152-176).  ``anchor`` is included only when captured —
    absence is honest evidence state, never fabricated."""
    signals: dict = {
        "after": {
            "outcome": action.after.outcome if action.after else "",
            "detail": action.after.detail if action.after else "",
            "navigated": bool(action.after.navigated) if action.after else False,
        },
        "url_changed": bool(action.url_changed),
        "qec": dict(action.qec or {}),
    }
    if action.anchor is not None:
        signals["anchor"] = {"label": action.anchor.label, "kind": action.anchor.kind}
    return signals


async def write_exploration(
    bundle: ExplorationBundle,
    *,
    tenant_id: str,
    artifact_id: str,
    session_id: str,
    extractor_version: str,
) -> WriteStats:
    """Atomically write one crawl's substrate rows (§2.1) and screenshots.

    Preconditions (created by ``artifacts.creator.create_crawl_artifact``):
    the tenant row and the ``canonical_artifacts`` row already exist.

    Raises:
        RefusalError:  an evidence rule is broken (honest 422 upstream) —
                       including a duplicate ``extractor_version`` write.
        RuntimeError:  infrastructure failure (missing artifact/tenant row,
                       storage failure) — an honest 500 upstream.
    """
    if not tenant_id or not artifact_id or not session_id:
        raise ValueError("tenant_id, artifact_id and session_id are required")
    version = validate_extractor_version(extractor_version)
    # Defence in depth: re-run every schema rule so mutated/`model_construct`
    # bundles (e.g. REFUSE-harness variants) can never reach the inserts.
    bundle = validate_bundle(bundle)

    # ── Screenshots first (non-transactional storage; §2.3 rule 3) ────────
    files = [
        (shot.frame_index, shot.decode(), shot.timestamp_ms / 1000.0)
        for page in bundle.pages
        for shot in page.screenshots
    ]
    frame_refs: list[FrameRef] = []
    if files:
        frame_refs = await store_screenshots(
            tenant_id, session_id, bundle.crawl_id, files,
        )
    ref_by_index = {ref.frame_index: ref for ref in frame_refs}

    now = utc_now()
    redaction_counts: dict[str, int] = {}
    unredacted_errors = 0

    visit_values: list[dict] = []
    action_values: list[dict] = []
    event_values: list[dict] = []
    frame_values: list[dict] = []
    event_seq = 0

    for page in bundle.pages:
        page_visit_id = new_id()
        snapshot, counts, errors = redacted_form_snapshot(page)
        for cls, n in counts.items():
            redaction_counts[cls] = redaction_counts.get(cls, 0) + n
        unredacted_errors += errors

        visit_values.append({
            "page_visit_id": page_visit_id,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "sequence_index": page.sequence_index,
            "location": page.location[:2000],
            "url_host": page.url_host[:500],
            "url_path": page.url_path[:2000],
            "url_query": page.url_query[:2000],
            "canonical_host": page.canonical_host[:500],
            "first_seen_ms": page.first_seen_ms,
            "last_seen_ms": page.last_seen_ms,
            "duration_ms": page.duration_ms,
            "frame_count": len(page.screenshots),
            "source": VISIT_SOURCE,
            "extraction_confidence": 1.0,
            "primary_scene_id": None,
            "form_snapshot": snapshot,
            "form_snapshot_signals": dict(page.form_snapshot_signals),
            # ANSWERS P1.B — rendered value nodes (text already scrubbed at capture).
            "displayed_values": list(page.displayed_values or []),
            "extractor_version": version,
            "form_snapshot_extractor_version": version,
            "created_at": now,
            "updated_at": now,
        })

        # Journal: the visit itself is a grounded navigation event.
        event_values.append({
            "event_id": _gt_event_id(artifact_id, event_seq, "navigate", page.first_seen_ms),
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "sequence_index": event_seq,
            "timestamp_ms": page.first_seen_ms,
            "kind": "navigate",
            "url": page.location[:2000],
            "url_host": page.url_host[:500],
            "url_path": page.url_path[:2000],
            "url_query": page.url_query[:2000],
            "target_label": "",
            "value": "",
            "target_kind": "",
            "modality": GT_MODALITY,
            "recorder_version": GT_RECORDER_VERSION,
            "signals": {"sequence_index": page.sequence_index},
            "created_at": now,
        })
        event_seq += 1

        for action in page.actions:
            result = redacted_action_value(action, page)
            merge_counts(redaction_counts, result)
            unredacted_errors += result.errors
            action_values.append({
                "page_action_id": new_id(),
                "page_visit_id": page_visit_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "subaction_index": action.subaction_index,
                "verb": action.verb,
                "target_label": action.target_label[:500],
                "target_kind": action.target_kind,
                "value": result.value if action.value is not None else None,
                "confidence": 1.0,
                "automation_ready": True,
                "reasoning": "",
                "evidence_signals": _evidence_signals(action),
                "extractor_model": "qe-explorer",
                "extractor_version": version,
                "generation_latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "created_at": now,
                "updated_at": now,
            })
            ts = action.timestamp_ms or page.first_seen_ms
            event_values.append({
                "event_id": _gt_event_id(artifact_id, event_seq, action.verb, ts),
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "sequence_index": event_seq,
                "timestamp_ms": ts,
                "kind": action.verb,
                "url": page.location[:2000],
                "url_host": page.url_host[:500],
                "url_path": page.url_path[:2000],
                "url_query": page.url_query[:2000],
                "target_label": action.target_label[:500],
                "value": result.value[:_ACTION_VALUE_MAX],
                "target_kind": action.target_kind[:40],
                "modality": GT_MODALITY,
                "recorder_version": GT_RECORDER_VERSION,
                "signals": {
                    "subaction_index": action.subaction_index,
                    "url_changed": bool(action.url_changed),
                },
                "created_at": now,
            })
            event_seq += 1

        for shot in page.screenshots:
            ref = ref_by_index[shot.frame_index]
            frame_values.append({
                "frame_id": new_id(),
                "tenant_id": tenant_id,
                "session_id": session_id,
                "job_id": f"{bundle.crawl_id}_frames",
                "video_id": None,
                "frame_index": shot.frame_index,
                "timestamp_seconds": shot.timestamp_ms / 1000.0,
                "frame_path": "",
                "frame_asset_path": ref.frame_asset_path,
                "artifact_id": artifact_id,
                "application_type": "web",
                "page_title": page.title[:500],
                "url_or_path": page.location[:1000],
                "ui_elements_json": [],
                "extracted_text": "",
                "tables_json": [],
                "state_changes_json": [],
                "description": "",
                "ocr_confidence": 0.0,
                "is_keyframe": True,
                "app_instance_id": None,
                "scene_id": None,
                "segmentation_confidence": 0.0,
                "created_at": now,
            })

    # ── ONE transaction (RLS GUC set inside — §2.1 write discipline) ──────
    async with tenant_scoped_substrate_session(tenant_id) as session:
        tenant_exists = (await session.execute(
            select(TenantRow.tenant_id).where(TenantRow.tenant_id == tenant_id)
        )).scalar_one_or_none()
        if tenant_exists is None:
            raise RuntimeError(
                f"tenant {tenant_id!r} is not bootstrapped — "
                f"create_crawl_artifact must run first"
            )
        artifact_exists = (await session.execute(
            select(CanonicalArtifactRow.artifact_id).where(
                CanonicalArtifactRow.artifact_id == artifact_id,
                CanonicalArtifactRow.tenant_id == tenant_id,
            )
        )).scalar_one_or_none()
        if artifact_exists is None:
            raise RuntimeError(
                f"artifact {artifact_id!r} not found for tenant {tenant_id!r} — "
                f"create_crawl_artifact must run first"
            )

        # UNIQUE-constraint safety (§2.3 rule 2): a version is written ONCE.
        already = (await session.execute(
            select(func.count()).select_from(PageVisitRow).where(
                PageVisitRow.artifact_id == artifact_id,
                PageVisitRow.extractor_version == version,
            )
        )).scalar_one()
        if already:
            raise RefusalError(
                "duplicate_extractor_version",
                f"{version!r} already has {already} page_visit row(s) on "
                f"artifact {artifact_id} — a re-crawl must mint a new "
                f"crawl_id (one write = one immutable version)",
            )

        if visit_values:
            await session.execute(insert(PageVisitRow), visit_values)
        if action_values:
            await session.execute(insert(PageActionRow), action_values)
        if event_values:
            # ground_truth_events is an IDEMPOTENT audit journal keyed by a
            # deterministic uuid5 event_id: the raw event happened once, so a
            # re-extraction of the SAME artifact under a new extractor_version
            # re-journals identical events — which must be a no-op, not a
            # duplicate-key crash. (page_visits/page_actions DO coexist across
            # versions via their version-scoped unique constraints; the journal
            # does not.) ON CONFLICT DO NOTHING makes re-extraction safe while
            # leaving a genuinely poisoned write (e.g. the R7 hijack's duplicate
            # sequence_index, which fails on the page_visits insert FIRST) to
            # still die atomically.
            await session.execute(
                pg_insert(GroundTruthEventRow)
                .values(event_values)
                .on_conflict_do_nothing(index_elements=["event_id"])
            )
        if frame_values:
            await session.execute(insert(VisualFrameRow), frame_values)

    stats = WriteStats(
        visits=len(visit_values),
        actions=len(action_values),
        screenshots=len(frame_refs),
        frames=len(frame_values),
        events=len(event_values),
        redactions=sum(redaction_counts.values()),
        unredacted_errors=unredacted_errors,
    )
    logger.info(
        "qec.substrate.write_completed",
        extra={
            "tenant_id": tenant_id,
            "artifact_id": artifact_id,
            "session_id": session_id,
            "extractor_version": version,
            "crawl_id": bundle.crawl_id,
            "stats": stats.model_dump(),
            "redaction_classes": redaction_counts,
        },
    )
    return stats
