"""Flywheel labeled-pair ledger (Phase 2 foundation).

A tenant-scoped, RLS-enforced, APPEND-ONLY record of human/outcome CORRECTIONS —
the substrate of the consented failure→fix flywheel. Every column is an
enum / bool / bucket / hash (see ``featurize``) — there is deliberately NO raw
text/value/selector/name column, so de-identification is enforced by the schema,
not just by callers.

``record_label`` is written INSIDE the caller's ``tenant_scoped_session`` so RLS
auto-stamps + isolates it exactly like ``script_versions``. It is BEST-EFFORT:
capture must never break the user's action, and it is gated default-OFF (the
caller checks the ``flywheel.capture`` flag) so zero-config behaviour is
byte-identical to today. Nothing here trains or exports — that is a separate,
consent-gated, default-OFF admin job.

ORM binds the SDK ``Base``; the table is created out-of-band by the idempotent
RLS migration ``scripts/apply_flywheel_labels.sql`` (mirrors script_versions).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, String, Index
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base

logger = logging.getLogger(__name__)

# The decision points that produce a labeled correction (see the Phase 2 design).
DECISION_POINTS = frozenset({
    "script_edit", "heal_approve", "triage_feedback", "heal_outcome",
    "value_conflict", "control_kind_fix", "reanchor", "scenario_lifecycle",
})


def _new_id() -> str:
    return uuid.uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FlywheelLabelRow(Base):
    """One de-identified labeled correction. Enum/bool/bucket/hash columns only."""

    __tablename__ = "flywheel_labels"

    flywheel_label_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)          # the RLS key
    artifact_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False)

    # Provenance — pin the example to the exact code that produced it.
    generator_version: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    git_commit: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    # Export gate (stamped at write) + the customer-declared vertical.
    consented: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    vertical_tag: Mapped[str] = mapped_column(String(40), default="unspecified", nullable=False)

    decision_point: Mapped[str] = mapped_column(String(32), nullable=False)

    # De-identified FEATURES (the model input) — all coarse.
    verb_enum: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    emitted_method_enum: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    observed_kind_enum: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    has_grounded_select: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    options_count_bucket: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    value_shape_sig: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    selector_drifted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bbox_drifted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_flaky: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_pattern_class: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    similarity_bucket: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    ambiguity_bucket: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    label_token_hash: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    diff_shape_enum: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    # The engine's call + the human/outcome CORRECTION (the label).
    engine_verdict_enum: Mapped[str] = mapped_column(String(24), default="", nullable=False)
    engine_confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    human_decision_enum: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    verified_green: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    later_contradicted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    __table_args__ = (
        Index("ix_flywheel_labels_tenant", "tenant_id"),
        Index("ix_flywheel_labels_export", "consented", "vertical_tag", "decision_point"),
    )


async def record_label(
    session: AsyncSession,
    *,
    tenant_id: str,
    decision_point: str,
    artifact_id: str = "",
    test_case_id: str = "",
    scenario_id: str = "",
    generator_version: str = "",
    git_commit: str = "",
    consented: bool = False,
    vertical_tag: str = "unspecified",
    # de-identified features (caller featurizes via flywheel.featurize)
    verb_enum: str = "",
    emitted_method_enum: str = "",
    observed_kind_enum: str = "",
    has_grounded_select: bool = False,
    options_count_bucket: str = "",
    value_shape_sig: str = "",
    selector_drifted: bool = False,
    bbox_drifted: bool = False,
    is_flaky: bool = False,
    error_pattern_class: str = "",
    similarity_bucket: str = "",
    ambiguity_bucket: str = "",
    label_token_hash: str = "",
    diff_shape_enum: str = "",
    # the engine call + the human/outcome correction (the label)
    engine_verdict_enum: str = "",
    engine_confidence: float = 0.0,
    human_decision_enum: str = "",
    verified_green: bool | None = None,
    later_contradicted: bool | None = None,
) -> None:
    """Append ONE de-identified labeled correction. Best-effort: a missing table
    (pre-migration) or any DB error is swallowed so capture never breaks the
    user's action. The caller must already hold ``session`` from
    ``tenant_scoped_session(tenant_id)`` so RLS stamps + isolates the row. Caller
    commits. No raw text/value/selector/name is accepted — only coarse features."""
    if not tenant_id or decision_point not in DECISION_POINTS:
        return
    try:
        session.add(FlywheelLabelRow(
            flywheel_label_id=_new_id(),
            tenant_id=tenant_id, artifact_id=artifact_id[:64],
            test_case_id=test_case_id[:64], scenario_id=scenario_id[:64],
            generator_version=generator_version[:64], git_commit=git_commit[:64],
            consented=bool(consented), vertical_tag=(vertical_tag or "unspecified")[:40],
            decision_point=decision_point,
            verb_enum=verb_enum[:24], emitted_method_enum=emitted_method_enum[:24],
            observed_kind_enum=observed_kind_enum[:24],
            has_grounded_select=bool(has_grounded_select),
            options_count_bucket=options_count_bucket[:8],
            value_shape_sig=value_shape_sig[:24],
            selector_drifted=bool(selector_drifted), bbox_drifted=bool(bbox_drifted),
            is_flaky=bool(is_flaky), error_pattern_class=error_pattern_class[:40],
            similarity_bucket=similarity_bucket[:8], ambiguity_bucket=ambiguity_bucket[:8],
            label_token_hash=label_token_hash[:64], diff_shape_enum=diff_shape_enum[:32],
            engine_verdict_enum=engine_verdict_enum[:24],
            engine_confidence=float(engine_confidence or 0.0),
            human_decision_enum=human_decision_enum[:32],
            verified_green=verified_green, later_contradicted=later_contradicted,
        ))
    except Exception as exc:  # pre-migration / DB error — capture is optional
        logger.debug("flywheel.record_label_skipped dp=%s err=%s", decision_point, exc)


__all__ = ["FlywheelLabelRow", "record_label", "DECISION_POINTS"]
