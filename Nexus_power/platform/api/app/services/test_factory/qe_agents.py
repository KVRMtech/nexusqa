"""QE agents — the four closed loops (Sentinel autonomy, Triage classifier,
Intent lens, precision publishing support).

Design contract shared by every function here:
  * deterministic first; the LLM may EXPLAIN or JUDGE-with-quotes but its
    output is validated (every quote must be a VERBATIM substring of the
    supplied evidence) and demoted to 'unverifiable' when validation fails;
  * best-effort persistence — an agent can never break the request path;
  * every claim carries evidence + confidence; 'unknown' is an honest verdict.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_sdk.db import Base
from ...database import tenant_scoped_session

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Sentinel: persisted alerts + autonomous daemon ───────────────────────────


class SentinelAlertRow(Base):
    """One deduplicated sentinel alert. dedupe_key keeps the daemon idempotent
    across cycles; resolved_at lets the inbox clear without deleting history."""

    __tablename__ = "sentinel_alerts"

    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="minor")
    artifact_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    test_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, nullable=False,
    )


async def sentinel_scan(tenant_id: str, *, stale_days: int = 14, persist: bool = True) -> list[dict]:
    """The Sentinel core: regression / spec-drift / stale detection over the
    verdict history. Pure read + optional idempotent persist (dedupe_key)."""
    from .verdict_events import VerdictEventRow, DecisionDossierRow

    alerts: list[dict] = []
    try:
        async with tenant_scoped_session(tenant_id) as session:
            rows = (await session.execute(
                select(VerdictEventRow).where(
                    VerdictEventRow.tenant_id == tenant_id
                ).order_by(VerdictEventRow.created_at.desc()).limit(1000)
            )).scalars().all()
            dossiers = (await session.execute(
                select(DecisionDossierRow.test_id, DecisionDossierRow.payload,
                       DecisionDossierRow.created_at).where(
                    DecisionDossierRow.tenant_id == tenant_id
                ).order_by(DecisionDossierRow.created_at.desc()).limit(500)
            )).all()
    except Exception:  # pragma: no cover
        logger.warning("sentinel.scan_read_failed", extra={"tenant_id": tenant_id})
        return []

    by_test: dict = {}
    for r in rows:
        by_test.setdefault((r.artifact_id, r.test_id), []).append(r)
    now = _utc_now()
    for (aid, tid), evs in by_test.items():
        if len(evs) >= 2 and evs[0].overall < evs[1].overall:
            alerts.append({"kind": "regression", "severity": "major",
                           "artifact_id": aid, "test_id": tid,
                           "dedupe_key": f"regression:{tid}:{evs[0].chain_hash[:12]}",
                           "evidence": f"overall {evs[1].overall} -> {evs[0].overall} at {evs[0].created_at.isoformat()}"})
        if evs and (now - evs[0].created_at).days >= stale_days:
            alerts.append({"kind": "stale", "severity": "minor",
                           "artifact_id": aid, "test_id": tid,
                           "dedupe_key": f"stale:{tid}:{(now - evs[0].created_at).days // stale_days}",
                           "evidence": f"no verdict in {(now - evs[0].created_at).days}d"})
    spec_hashes: dict = {}
    for tid, payload, _created in dossiers:
        h = ((payload or {}).get("inputs") or {}).get("spec_sha256")
        if not h:
            continue
        prev = spec_hashes.get(tid)
        if prev and prev != h:
            alerts.append({"kind": "spec_drift", "severity": "advisory",
                           "test_id": tid, "artifact_id": "",
                           "dedupe_key": f"drift:{tid}:{h[:12]}",
                           "evidence": f"spec hash changed {prev[:10]} -> {h[:10]} between verifies"})
        spec_hashes.setdefault(tid, h)

    if persist and alerts:
        try:
            async with tenant_scoped_session(tenant_id) as session:
                existing = set((await session.execute(
                    select(SentinelAlertRow.dedupe_key).where(
                        SentinelAlertRow.tenant_id == tenant_id).limit(5000)
                )).scalars().all())
                fresh = 0
                for a in alerts:
                    if a["dedupe_key"] in existing:
                        continue
                    session.add(SentinelAlertRow(
                        alert_id=uuid.uuid4().hex, tenant_id=tenant_id,
                        dedupe_key=a["dedupe_key"], kind=a["kind"],
                        severity=a["severity"], artifact_id=a.get("artifact_id", ""),
                        test_id=a.get("test_id", ""),
                        evidence=a["evidence"][:500]))
                    fresh += 1
                await session.commit()
                if fresh:
                    logger.info("sentinel.alerts_persisted",
                                extra={"tenant_id": tenant_id, "fresh": fresh})
        except Exception:  # pragma: no cover
            logger.warning("sentinel.persist_failed", extra={"tenant_id": tenant_id})
    return alerts


async def sentinel_daemon() -> None:
    """AUTONOMY: scheduled Sentinel. Enabled when NEXUS_SENTINEL_INTERVAL_MIN>0
    (default 60). Tenant from NEXUS_SENTINEL_TENANT (default nexus-platform).
    Fully guarded — a cycle failure never kills the loop."""
    try:
        interval = int(os.getenv("NEXUS_SENTINEL_INTERVAL_MIN", "60") or 60)
    except ValueError:
        interval = 60
    tenant = os.getenv("NEXUS_SENTINEL_TENANT", "nexus-platform")
    if interval <= 0:
        logger.info("sentinel.daemon_disabled")
        return
    logger.warning("sentinel.daemon_started",
                extra={"interval_min": interval, "tenant": tenant})
    while True:
        try:
            found = await sentinel_scan(tenant, persist=True)
            logger.info("sentinel.cycle", extra={"alerts": len(found)})
        except Exception:  # pragma: no cover
            logger.warning("sentinel.cycle_failed")
        await asyncio.sleep(max(60, interval * 60))


# ── Triage: deterministic failure classifier ─────────────────────────────────

_TRIAGE_RULES = [
    # (class, confidence, [substring markers over the job json])
    ("product_defect", 0.9, ["real_regression", "defect_report", "http 5", "status\": 5"]),
    ("environment", 0.85, ["auth_required", "relogin", "unreachable", "econnrefused",
                            "dns", "net::err", "timeout exceeded while navigating",
                            "certificate"]),
    ("script_defect", 0.8, ["strict mode violation", "ambiguous", "locator resolved 0",
                             "not found", "reanchor", "waiting for locator"]),
    ("data", 0.75, ["validation", "required field", "invalid value", "duplicate"]),
]


def triage_classify(job_json: dict | str) -> dict:
    """Product vs script vs environment vs data — deterministic v1 over the
    runner job record. Honest 'unknown' when no rule matches; healed-green runs
    are classified script_defect(healed) because the drift was in the script's
    binding, not the product."""
    raw = job_json if isinstance(job_json, str) else json.dumps(job_json, default=str)
    text = raw.casefold()
    status = ""
    if isinstance(job_json, dict):
        status = str(job_json.get("status") or "").casefold()

    if status == "passed" or "\"clean_run\"" in text or "prove-green" in text and "passed" in text:
        healed = "heal" in text and ("applied" in text or "healed" in text)
        return {"classification": "script_defect" if healed else "none",
                "subtype": "healed_drift" if healed else "no_failure",
                "confidence": 0.85 if healed else 0.99,
                "evidence": ("run green after heal — the drift was in the script "
                             "binding, not the product" if healed
                             else "run passed without healing"),
                "classifier": "triage-v1-deterministic"}

    hits = []
    for cls, conf, markers in _TRIAGE_RULES:
        matched = [m for m in markers if m in text]
        if matched:
            hits.append({"classification": cls, "confidence": conf,
                         "evidence": f"markers: {matched[:3]}"})
    if hits:
        best = max(hits, key=lambda h: h["confidence"])
        best["alternatives"] = [h for h in hits if h is not best][:3]
        best["classifier"] = "triage-v1-deterministic"
        return best
    return {"classification": "unknown", "confidence": 0.3,
            "evidence": "no deterministic marker matched — honest unknown; "
                        "route to human with the diagnostics bundle",
            "classifier": "triage-v1-deterministic"}


# ── Intent lens: quote-grounded requirement judgment ─────────────────────────


def build_intent_evidence(tc, carry: list | None = None) -> str:
    """The ONLY text the LLM may quote from: the case's demonstrated steps,
    values and outcome. Verbatim-substring validation runs against this."""
    lines = [f"CASE: {getattr(tc, 'name', '')}"]
    for s in list(getattr(tc, "steps", []) or [])[:60]:
        a = s.get("action") if isinstance(s, dict) else getattr(s, "action", "")
        if a:
            lines.append(f"STEP: {a}")
    eo = getattr(tc, "expected_outcome", "") or ""
    if eo:
        lines.append(f"OUTCOME: {eo}")
    for x in (carry or [])[:6]:
        lines.append(f"DATA-CARRY: '{x.get('value')}' from '{x.get('entered_on')}' "
                     f"re-appears on '{x.get('reappears_on')}'")
    return "\n".join(lines)


def intent_heuristic(requirement_text: str, evidence_text: str) -> dict:
    """Deterministic fallback: token coverage of the requirement inside the
    demonstrated evidence. Honest 'heuristic' labeling — never a fake LLM."""
    import re
    toks = {t for t in re.findall(r"[a-z0-9]{4,}", requirement_text.casefold())}
    ev = evidence_text.casefold()
    hit = sorted(t for t in toks if t in ev)
    cov = round(len(hit) / len(toks), 3) if toks else 0.0
    verdict = ("satisfies" if cov >= 0.75 else
               "partial" if cov >= 0.4 else "unsatisfied")
    return {"verdict": verdict, "coverage": cov,
            "matched_terms": hit[:15],
            "missing_terms": sorted(toks - set(hit))[:15],
            "rationale": (f"heuristic token coverage {cov}: {len(hit)}/{len(toks)} "
                          "requirement terms demonstrated in the recording evidence"),
            "quotes": [], "mode": "heuristic-deterministic"}


def validate_intent_quotes(result: dict, evidence_text: str) -> dict:
    """Every LLM quote must be a VERBATIM substring of the evidence — else the
    whole judgment demotes to 'unverifiable'. The agent never grades itself."""
    quotes = [q for q in (result.get("quotes") or []) if isinstance(q, str)]
    bad = [q for q in quotes if q not in evidence_text]
    if bad or not quotes:
        return {"verdict": "unverifiable",
                "rationale": ("LLM judgment demoted: "
                              + (f"{len(bad)} quote(s) not verbatim in evidence"
                                 if bad else "no evidence quotes supplied")),
                "quotes": [], "mode": "llm-demoted"}
    result["mode"] = "llm-grounded"
    return result
