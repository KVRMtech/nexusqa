"""Verdict history — the per-script quality timeline (AI Verification Platform §9).

One immutable row per verification verdict, keyed to (tenant, artifact, test,
version). Written by POST .../verify and by the delivery gate on every zip
download, so the timeline reflects what was actually measured and what actually
shipped. Rows are HASH-CHAINED per (tenant, artifact, test) — each event's
chain_hash = sha256(previous chain_hash + canonical payload) — the same
tamper-evidence posture as heal_events, so a regulator can replay the chain.

ORM binds the SDK ``Base`` (mirrors runner_jobs / script_versions); the table is
created out-of-band (scripts/create_verdict_events.py, checkfirst). Every helper
is best-effort and tenant-scoped in its WHERE clauses; a missing table degrades
to a no-op so verification itself can never fail on history bookkeeping."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base
from ...database import tenant_scoped_session

logger = logging.getLogger(__name__)

REGISTRY_VERSION = "auditor-v1+lint-v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class VerdictEventRow(Base):
    """One verification verdict for one script version. Immutable, hash-chained."""

    __tablename__ = "verdict_events"

    verdict_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    test_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registry_version: Mapped[str] = mapped_column(String(64), nullable=False, default=REGISTRY_VERSION)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="verify")
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    overall: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    axes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    gaps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    findings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    lint: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    preflight: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


def _canonical_payload(row: dict) -> str:
    core = {k: row.get(k) for k in (
        "tenant_id", "artifact_id", "test_id", "version", "registry_version",
        "source", "overall", "decision", "axes", "gaps",
    )}
    return json.dumps(core, sort_keys=True, separators=(",", ":"), default=str)


async def record_verdict(
    *,
    tenant_id: str,
    artifact_id: str,
    test_id: str,
    version: int | None,
    source: str,
    actor: str,
    overall: int,
    decision: str,
    axes: dict,
    gaps: int = 0,
    findings: list | None = None,
    lint: list | None = None,
    preflight: dict | None = None,
) -> dict | None:
    """Append one hash-chained verdict event. Best-effort: any failure (missing
    table, transient DB error) logs and returns None — verification never
    fails on history bookkeeping."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            prev = (await session.execute(
                select(VerdictEventRow.chain_hash)
                .where(
                    VerdictEventRow.tenant_id == tenant_id,
                    VerdictEventRow.artifact_id == artifact_id,
                    VerdictEventRow.test_id == test_id,
                )
                .order_by(VerdictEventRow.created_at.desc())
                .limit(1)
            )).scalar() or ""
            payload = {
                "tenant_id": tenant_id, "artifact_id": artifact_id,
                "test_id": test_id, "version": version,
                "registry_version": REGISTRY_VERSION, "source": source,
                "overall": int(overall), "decision": str(decision or ""),
                "axes": dict(axes or {}), "gaps": int(gaps or 0),
            }
            chain = hashlib.sha256(
                (prev + _canonical_payload(payload)).encode("utf-8")).hexdigest()
            row = VerdictEventRow(
                verdict_id=uuid.uuid4().hex,
                tenant_id=tenant_id,
                artifact_id=artifact_id,
                test_id=test_id,
                version=version,
                registry_version=REGISTRY_VERSION,
                source=source,
                actor=(actor or "")[:200],
                overall=int(overall),
                decision=str(decision or "")[:32],
                axes=dict(axes or {}),
                gaps=int(gaps or 0),
                findings=list(findings or [])[:20],
                lint=list(lint or [])[:20],
                preflight=preflight,
                chain_hash=chain,
            )
            session.add(row)
            await session.commit()
            return {"verdict_id": row.verdict_id, "chain_hash": chain}
    except Exception:  # pragma: no cover — best-effort by contract
        logger.warning(
            "verdict_events.record_failed",
            extra={"artifact_id": artifact_id, "test_id": test_id, "source": source},
        )
        return None


async def list_verdicts(
    *,
    tenant_id: str,
    artifact_id: str,
    test_id: str,
    limit: int = 50,
) -> list[dict]:
    """Newest-first verdict timeline for one script. Best-effort ([] on error)."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            rows = (await session.execute(
                select(VerdictEventRow)
                .where(
                    VerdictEventRow.tenant_id == tenant_id,
                    VerdictEventRow.artifact_id == artifact_id,
                    VerdictEventRow.test_id == test_id,
                )
                .order_by(VerdictEventRow.created_at.desc())
                .limit(max(1, min(200, limit)))
            )).scalars().all()
        return [{
            "verdict_id": r.verdict_id,
            "version": r.version,
            "registry_version": r.registry_version,
            "source": r.source,
            "actor": r.actor,
            "overall": r.overall,
            "decision": r.decision,
            "axes": r.axes,
            "gaps": r.gaps,
            "findings_count": len(r.findings or []),
            "chain_hash": r.chain_hash,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows]
    except Exception:  # pragma: no cover
        logger.warning(
            "verdict_events.list_failed",
            extra={"artifact_id": artifact_id, "test_id": test_id},
        )
        return []


def trend(events_newest_first: list[dict]) -> dict:
    """Regression-aware trend summary over a newest-first timeline."""
    if not events_newest_first:
        return {"count": 0}
    latest = events_newest_first[0]
    previous = events_newest_first[1] if len(events_newest_first) > 1 else None
    delta = (latest["overall"] - previous["overall"]) if previous else None
    return {
        "count": len(events_newest_first),
        "latest_overall": latest["overall"],
        "latest_decision": latest["decision"],
        "previous_overall": previous["overall"] if previous else None,
        "delta": delta,
        "regression": bool(previous and latest["overall"] < previous["overall"]),
    }


# ── Decision dossiers (§10) ──────────────────────────────────────────────────


class DecisionDossierRow(Base):
    """Reproducible decision record for one verdict. Immutable; chained to the
    verdict event. Replaying the same inputs at the same registry version
    reproduces the verdict byte-for-byte — this row proves what was decided,
    from what, under which rules."""

    __tablename__ = "decision_dossiers"

    verdict_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    test_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


async def save_dossier(
    *, tenant_id: str, artifact_id: str, test_id: str,
    verdict_id: str, chain_hash: str, payload: dict,
) -> bool:
    """Best-effort dossier write (missing table degrades to no-op)."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            session.add(DecisionDossierRow(
                verdict_id=verdict_id, tenant_id=tenant_id,
                artifact_id=artifact_id, test_id=test_id,
                payload=payload, chain_hash=chain_hash,
            ))
            await session.commit()
        return True
    except Exception:  # pragma: no cover
        logger.warning("verdict_events.dossier_save_failed",
                       extra={"verdict_id": verdict_id})
        return False


async def get_dossier(*, tenant_id: str, verdict_id: str) -> dict | None:
    try:
        async with tenant_scoped_session(tenant_id) as session:
            row = (await session.execute(
                select(DecisionDossierRow).where(
                    DecisionDossierRow.verdict_id == verdict_id,
                    DecisionDossierRow.tenant_id == tenant_id,
                )
            )).scalar()
        if row is None:
            return None
        return {"verdict_id": row.verdict_id, "artifact_id": row.artifact_id,
                "test_id": row.test_id, "chain_hash": row.chain_hash,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "payload": row.payload}
    except Exception:  # pragma: no cover
        return None


# ── Waivers (governed exceptions — never silent) ─────────────────────────────


class WaiverRow(Base):
    """A governed exception for a finding: owner + reason + expiry, always
    surfaced in verdicts as WAIVED (never removed). Expired waivers stop
    applying automatically."""

    __tablename__ = "verification_waivers"

    waiver_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    test_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    finding_match: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


async def create_waiver(
    *, tenant_id: str, artifact_id: str, test_id: str,
    finding_match: str, reason: str, actor: str, expires_at: datetime,
) -> dict | None:
    try:
        async with tenant_scoped_session(tenant_id) as session:
            row = WaiverRow(
                waiver_id=uuid.uuid4().hex, tenant_id=tenant_id,
                artifact_id=artifact_id, test_id=test_id,
                finding_match=(finding_match or "")[:300],
                reason=(reason or "")[:500], actor=(actor or "")[:200],
                expires_at=expires_at,
            )
            session.add(row)
            await session.commit()
            return {"waiver_id": row.waiver_id, "expires_at": expires_at.isoformat()}
    except Exception:  # pragma: no cover
        logger.warning("verdict_events.waiver_create_failed",
                       extra={"artifact_id": artifact_id})
        return None


async def active_waivers(*, tenant_id: str, artifact_id: str, test_id: str) -> list[dict]:
    try:
        async with tenant_scoped_session(tenant_id) as session:
            rows = (await session.execute(
                select(WaiverRow).where(
                    WaiverRow.tenant_id == tenant_id,
                    WaiverRow.artifact_id == artifact_id,
                    WaiverRow.test_id == test_id,
                    WaiverRow.expires_at > _utc_now(),
                ).order_by(WaiverRow.created_at.desc())
            )).scalars().all()
        return [{"waiver_id": r.waiver_id, "finding_match": r.finding_match,
                 "reason": r.reason, "actor": r.actor,
                 "expires_at": r.expires_at.isoformat()} for r in rows]
    except Exception:  # pragma: no cover
        return []


def apply_waivers(findings: list, waivers: list[dict]) -> tuple[list, list]:
    """(active_findings, waived_findings) — a waiver ANNOTATES, never deletes.
    Match = case-insensitive substring on the finding text."""
    active, waived = [], []
    for f in findings or []:
        txt = str(f).casefold()
        hit = next((w for w in waivers
                    if w.get("finding_match", "").casefold() in txt
                    and w.get("finding_match")), None)
        if hit:
            waived.append({"finding": f, "waiver_id": hit["waiver_id"],
                           "reason": hit["reason"], "expires_at": hit["expires_at"]})
        else:
            active.append(f)
    return active, waived


# ── Production-risk model v1 (§6) ────────────────────────────────────────────


def risk(*, steps: list, det: dict, lint: list, preflight: dict | None) -> dict:
    """Deterministic risk = likelihood x blast_radius / detectability.
    Drivers are named + evidence-linked; thresholds fixed and versioned."""
    n_steps = max(1, len(steps or []))
    gaps = int(det.get("gaps") or 0)
    lint_err = sum(1 for l in (lint or []) if l.get("severity") == "error")
    amb = 0
    if isinstance(preflight, dict):
        out = json.dumps(preflight)[:20000].casefold()
        amb = out.count("ambiguous") + out.count('"0"')
    likelihood = min(1.0, 0.1 + 0.6 * (gaps / n_steps) + 0.2 * min(1, lint_err)
                     + 0.1 * min(1, amb / 3))

    nav_pages = sum(
        1 for s in (steps or [])
        if "navigate" in str(
            (s.get("observed", {}) if isinstance(s, dict)
             else getattr(s, "observed", None) or {})
        ).casefold() or "Open " in str(
            s.get("action") if isinstance(s, dict) else getattr(s, "action", "")))
    blast = min(1.0, 0.2 + 0.8 * (nav_pages / max(1, n_steps)))

    hard_asserts = sum(
        1 for p in (det.get("per_step") or []) if p.get("verdict") == "grounded_ok")
    detectability = max(0.2, min(1.0, hard_asserts / n_steps))

    score = round(min(1.0, likelihood * blast / detectability), 3)
    level = "LOW" if score < 0.15 else ("MEDIUM" if score < 0.4 else "HIGH")
    return {
        "model_version": "risk-v1",
        "score": score,
        "level": level,
        "drivers": [
            {"factor": "likelihood", "value": round(likelihood, 3),
             "evidence": f"{gaps} gaps / {n_steps} steps, {lint_err} lint errors, {amb} preflight ambiguities"},
            {"factor": "blast_radius", "value": round(blast, 3),
             "evidence": f"{nav_pages} page transitions in flow"},
            {"factor": "detectability", "value": round(detectability, 3),
             "evidence": f"{hard_asserts} grounded assertions / {n_steps} steps"},
        ],
    }


def build_dossier(
    *, spec: str, steps: list, det: dict, lint: list,
    preflight: dict | None, risk_obj: dict, alternatives: list,
    actor: str, source: str,
) -> dict:
    """The reproducible decision record. Rationale is TEMPLATE-rendered from
    measured fields — never free prose, so replays are byte-stable."""
    spec_hash = hashlib.sha256((spec or "").encode("utf-8")).hexdigest()
    steps_hash = hashlib.sha256(
        json.dumps([_canonical_step(s) for s in (steps or [])],
                   sort_keys=True, default=str).encode("utf-8")).hexdigest()
    dims = dict(det.get("dimension_scores") or {})
    rationale = (
        f"decision={det.get('decision')} because overall={det.get('overall_score')} "
        f"(min-gated over {len(dims)} dimensions; lowest="
        f"{min(dims, key=dims.get) if dims else 'n/a'}:{min(dims.values()) if dims else 'n/a'}), "
        f"gaps={det.get('gaps')} declared honest UNPROVEN, "
        f"lint_errors={sum(1 for l in (lint or []) if l.get('severity') == 'error')}, "
        f"risk={risk_obj.get('level')}({risk_obj.get('score')}), "
        f"registry={REGISTRY_VERSION}."
    )
    return {
        "dossier_version": "v1",
        "inputs": {
            "spec_sha256": spec_hash,
            "steps_sha256": steps_hash,
            "registry_version": REGISTRY_VERSION,
            "source": source,
            "actor": actor,
        },
        "rules_applied": {
            "dimensions": sorted(dims.keys()),
            "lint_rules_fired": sorted({l.get("rule", "") for l in (lint or [])}),
            "decision_rule": "min-gated deterministic (LLM never scores)",
        },
        "evidence": {
            "per_step_verdicts": list(det.get("per_step") or [])[:60],
            "preflight": preflight,
        },
        "alternatives_considered": alternatives[:40],
        "risk": risk_obj,
        "final_rationale": rationale,
    }


def _canonical_step(s) -> dict:
    if isinstance(s, dict):
        return {"n": s.get("step_number"), "a": s.get("action"),
                "p": s.get("provenance")}
    return {"n": getattr(s, "step_number", None),
            "a": getattr(s, "action", None),
            "p": getattr(s, "provenance", None)}


# ── Finding-outcome labels (Historian fuel) ──────────────────────────────────


class FindingLabelRow(Base):
    """A human/run-confirmed outcome for one finding: the fuel that turns
    check confidence from heuristic into MEASURED precision."""

    __tablename__ = "finding_labels"

    label_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    test_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dimension: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    finding_match: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="")  # confirmed|refuted
    actor: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


async def add_finding_label(
    *, tenant_id: str, artifact_id: str, test_id: str,
    dimension: str, finding_match: str, outcome: str, actor: str,
) -> dict | None:
    try:
        async with tenant_scoped_session(tenant_id) as session:
            row = FindingLabelRow(
                label_id=uuid.uuid4().hex, tenant_id=tenant_id,
                artifact_id=artifact_id, test_id=test_id,
                dimension=(dimension or "")[:64],
                finding_match=(finding_match or "")[:300],
                outcome=outcome, actor=(actor or "")[:200])
            session.add(row)
            await session.commit()
            return {"label_id": row.label_id}
    except Exception:  # pragma: no cover
        logger.warning("verdict_events.label_failed", extra={"artifact_id": artifact_id})
        return None


async def precision_by_dimension(*, tenant_id: str) -> dict:
    """Measured precision per dimension where labels exist; honest None where
    they do not. precision = confirmed / (confirmed + refuted)."""
    try:
        async with tenant_scoped_session(tenant_id) as session:
            rows = (await session.execute(
                select(FindingLabelRow.dimension, FindingLabelRow.outcome)
                .where(FindingLabelRow.tenant_id == tenant_id).limit(5000)
            )).all()
    except Exception:  # pragma: no cover
        return {}
    agg: dict = {}
    for dim, outcome in rows:
        d = agg.setdefault(dim or "unspecified", {"confirmed": 0, "refuted": 0})
        if outcome in d:
            d[outcome] += 1
    for d in agg.values():
        tot = d["confirmed"] + d["refuted"]
        d["precision"] = round(d["confirmed"] / tot, 3) if tot else None
        d["labels"] = tot
    return agg
