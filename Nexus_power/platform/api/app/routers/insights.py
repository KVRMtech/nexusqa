"""
Platform API — Executive Insights routes (Module 11).

Real aggregated data from PostgreSQL — KPIs, ROI, risks, weekly trends,
engine status.
"""
from __future__ import annotations

from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select, func, desc, and_

from nexus_sdk.db.models import (
    SessionRow, TestSuiteRow, TestRunRow,
    ContradictionRow, JurisdictionRow,
)

from ..database import require_db, utc_now
from ..auth import get_current_user

router = APIRouter(tags=["Insights"])

# ─── Module-level state (set at app init) ─────────────────────
_http: httpx.AsyncClient | None = None
_cache = None
_config = None


def init_insights(http_client: httpx.AsyncClient, cache, config) -> None:
    """Initialise module-level singletons used by insight endpoints."""
    global _http, _cache, _config
    _http = http_client
    _cache = cache
    _config = config


# ─── KPIs ─────────────────────────────────────────────────────

@router.get("/api/v1/insights/kpis")
async def get_kpis(user: dict = Depends(get_current_user)):
    """KPIs aggregated from real PostgreSQL data."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        sess_count = await db.scalar(
            select(func.count()).select_from(SessionRow)
            .where(SessionRow.tenant_id == tenant_id)
        ) or 0
        total_rules = await db.scalar(
            select(func.coalesce(func.sum(SessionRow.rules_extracted), 0))
            .where(SessionRow.tenant_id == tenant_id)
        ) or 0
        avg_pass_rate = await db.scalar(
            select(func.coalesce(func.avg(TestSuiteRow.pass_rate), 0.0))
            .where(TestSuiteRow.tenant_id == tenant_id)
        ) or 0.0
        suite_count = await db.scalar(
            select(func.count()).select_from(TestSuiteRow)
            .where(TestSuiteRow.tenant_id == tenant_id)
        ) or 0
        total_failed = await db.scalar(
            select(func.coalesce(func.sum(TestRunRow.failed), 0))
            .where(TestRunRow.tenant_id == tenant_id)
        ) or 0
        open_contradictions = await db.scalar(
            select(func.count()).select_from(ContradictionRow)
            .where(and_(
                ContradictionRow.tenant_id == tenant_id,
                ContradictionRow.status == "open",
            ))
        ) or 0
        week_ago = utc_now() - timedelta(days=7)
        recent_rules = await db.scalar(
            select(func.coalesce(func.sum(SessionRow.rules_extracted), 0))
            .where(and_(
                SessionRow.tenant_id == tenant_id,
                SessionRow.created_at >= week_ago,
            ))
        ) or 0

    knowledge_items = sess_count + int(total_rules)
    return [
        {"label": "Knowledge Items", "value": str(knowledge_items), "change": f"+{recent_rules} this week", "trend": "up" if recent_rules > 0 else "flat"},
        {"label": "Test Coverage", "value": f"{avg_pass_rate:.1f}%", "change": f"{suite_count} suites tracked", "trend": "up" if avg_pass_rate > 70 else "down"},
        {"label": "Defects Found", "value": str(total_failed), "change": f"{open_contradictions} contradictions open", "trend": "up" if total_failed > 0 else "flat"},
        {"label": "Risk Grade", "value": "A" if open_contradictions == 0 else "B+" if open_contradictions <= 3 else "B" if open_contradictions <= 5 else "C", "change": f"{open_contradictions} open risks", "trend": "up" if open_contradictions <= 3 else "down"},
    ]


# ─── ROI ──────────────────────────────────────────────────────

@router.get("/api/v1/insights/roi")
async def get_roi(user: dict = Depends(get_current_user)):
    """ROI calculated from real platform data."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        sess_count = await db.scalar(
            select(func.count()).select_from(SessionRow)
            .where(SessionRow.tenant_id == tenant_id)
        ) or 0
        total_tests = await db.scalar(
            select(func.coalesce(func.sum(TestSuiteRow.test_count), 0))
            .where(TestSuiteRow.tenant_id == tenant_id)
        ) or 0
        defects_caught = await db.scalar(
            select(func.coalesce(func.sum(TestRunRow.failed), 0))
            .where(TestRunRow.tenant_id == tenant_id)
        ) or 0
        violations_prevented = await db.scalar(
            select(func.count()).select_from(ContradictionRow)
            .where(ContradictionRow.tenant_id == tenant_id)
        ) or 0

    hours_saved = sess_count * 2
    doc_cost = hours_saved * 150
    test_cost = int(total_tests) * 150
    defect_cost = defects_caught * 8000
    compliance_cost = violations_prevented * 150000
    total_roi = doc_cost + test_cost + defect_cost + compliance_cost

    return [
        {"label": "Hours Saved (KT Documentation)", "value": f"{hours_saved} hrs", "equivalent": f"${doc_cost:,}", "period": "All Time"},
        {"label": "Test Cases Auto-Generated", "value": f"{int(total_tests)} tests", "equivalent": f"${test_cost:,}", "period": "All Time"},
        {"label": "Defects Caught Pre-Production", "value": f"{defects_caught} defects", "equivalent": f"${defect_cost:,}", "period": "All Time"},
        {"label": "Compliance Issues Prevented", "value": f"{violations_prevented} issues", "equivalent": f"${compliance_cost:,}", "period": "All Time"},
        {"label": "Total ROI", "value": "", "equivalent": f"${total_roi:,}", "period": "", "highlight": True},
    ]


# ─── Risks ────────────────────────────────────────────────────

@router.get("/api/v1/insights/risks")
async def get_risks(user: dict = Depends(get_current_user)):
    """Real risks from open contradictions, failed tests, and low compliance."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    risks = []
    async with factory() as db:
        result = await db.execute(
            select(ContradictionRow)
            .where(and_(ContradictionRow.tenant_id == tenant_id, ContradictionRow.status == "open"))
            .order_by(desc(ContradictionRow.created_at))
            .limit(10)
        )
        for row in result.scalars().all():
            risks.append({
                "id": row.contradiction_id,
                "title": row.description[:100] if row.description else "Unresolved Contradiction",
                "severity": row.severity, "domain": "Knowledge", "link": "/contradictions",
            })
        result = await db.execute(
            select(TestRunRow)
            .where(and_(TestRunRow.tenant_id == tenant_id, TestRunRow.failed > 0))
            .order_by(desc(TestRunRow.started_at)).limit(5)
        )
        for row in result.scalars().all():
            risks.append({
                "id": row.run_id,
                "title": f"Test Run: {row.failed} failures in {row.total_tests} tests",
                "severity": "high" if row.failed > 5 else "medium", "domain": "Testing", "link": "/test-center",
            })
        result = await db.execute(
            select(JurisdictionRow)
            .where(and_(JurisdictionRow.tenant_id == tenant_id, JurisdictionRow.compliance_score < 80.0))
            .order_by(JurisdictionRow.compliance_score).limit(5)
        )
        for row in result.scalars().all():
            risks.append({
                "id": row.jurisdiction_id,
                "title": f"{row.name} Compliance Score: {row.compliance_score:.0f}%",
                "severity": "critical" if row.compliance_score < 50 else "high",
                "domain": "Compliance", "link": "/compliance",
            })
    return risks


# ─── Weekly Trend ─────────────────────────────────────────────

@router.get("/api/v1/insights/weekly-trend")
async def get_weekly_trend(user: dict = Depends(get_current_user)):
    """Real weekly trend from actual activity."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        trends = []
        now = utc_now()
        for week_offset in range(7, -1, -1):
            week_start = now - timedelta(weeks=week_offset + 1)
            week_end = now - timedelta(weeks=week_offset)
            label = f"W{8 - week_offset}"
            rules = await db.scalar(
                select(func.coalesce(func.sum(SessionRow.rules_extracted), 0))
                .where(and_(SessionRow.tenant_id == tenant_id, SessionRow.created_at >= week_start, SessionRow.created_at < week_end))
            ) or 0
            tests = await db.scalar(
                select(func.coalesce(func.sum(TestRunRow.total_tests), 0))
                .where(and_(TestRunRow.tenant_id == tenant_id, TestRunRow.started_at >= week_start, TestRunRow.started_at < week_end))
            ) or 0
            defects = await db.scalar(
                select(func.coalesce(func.sum(TestRunRow.failed), 0))
                .where(and_(TestRunRow.tenant_id == tenant_id, TestRunRow.started_at >= week_start, TestRunRow.started_at < week_end))
            ) or 0
            trends.append({"week": label, "rules": int(rules), "tests": int(tests), "defects": int(defects)})
    return trends


# ─── Engine Status ────────────────────────────────────────────

@router.get("/api/v1/insights/engines")
async def get_engine_status(user: dict = Depends(get_current_user)):
    async def _fetch():
        engine_map = {
            "Ears (Speech)": _config.ears_engine_url,
            "Eyes (Vision)": _config.eyes_engine_url,
            "Heart (LLM)": _config.heart_engine_url,
            "Backbone (Graph)": _config.backbone_engine_url,
            "Shield (Security)": _config.shield_engine_url,
            "Hands (Test Gen)": _config.hands_engine_url,
            "Legs (Execute)": _config.legs_engine_url,
            "Mouth (Reports)": _config.mouth_engine_url,
            "Spine (Orchestrator)": _config.spine_engine_url,
            "Nerves (Connectors)": _config.nerves_engine_url,
            "Brain (Coordinator)": _config.brain_engine_url,
        }
        results = []
        for name, url in engine_map.items():
            try:
                resp = await _http.get(f"{url}/health", timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    results.append({"name": name, "status": "online" if data.get("status") == "healthy" else "degraded", "load": data.get("load", 0)})
                else:
                    results.append({"name": name, "status": "degraded", "load": 0})
            except Exception:
                results.append({"name": name, "status": "offline", "load": 0})
        return results
    return await _cache.get_or_set("engine_status", _fetch, ttl=30)
