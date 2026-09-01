"""QE-Central — REFUSE-matrix runner (design §3.1 harness/runner).

For each selected rule the runner:
  1. creates a FRESH artifact via ``artifacts.creator`` (fresh crawl_id →
     fresh ``qec_live_v1@…`` version — no cross-rule contamination),
  2. applies the rule's ``fixture_mutation`` to the golden bundle,
  3. writes the substrate through the REAL ``substrate.writer`` (honoring
     the rule's write policy — an honest writer refusal is evidence too),
  4. drives the UNCHANGED VKPower factory chain over HTTP
     (``PLATFORM_API_URL``) with a minted service JWT,
  5. classifies the outcome (``rules.classify_verdict``) and persists one
     ``qe_harness_runs`` row with the FULL HTTP/readback evidence JSONB.

Process contract (CI/deploy gate): ``python -m app.harness.runner`` exits
  * 2 when any rule is ``GREEN_WASH_DETECTED``  (non-zero, per the pinned
    contract "non-zero when any GREEN_WASH_DETECTED"),
  * 1 when honesty could NOT be proven (any ``CHAIN_ERROR`` / failed
    baseline) — a gate that cannot prove honesty must not pass silently,
  * 0 only when the baseline passed and every rule refused correctly.

Env knobs (all optional, development-safe defaults):
  ``QEC_HARNESS_TENANT_ID``                 CLI tenant (default qec-harness-tenant)
  ``QEC_HARNESS_HTTP_TIMEOUT_SECONDS``      per-request timeout (default 300 —
                                            /verify runs a live preflight up to 120s)
  ``QEC_HARNESS_POLL_INTERVAL_SECONDS``     auto-heal poll interval (default 5)
  ``QEC_HARNESS_POLL_TIMEOUT_SECONDS``      auto-heal poll budget (default 600)
  ``QEC_HARNESS_UNREACHABLE_BASE_URL``      dead-environment URL (default
                                            RFC 5737 TEST-NET-1, see rules.py)
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import io
import json
import logging
import os
import sys
import uuid
import zipfile
from datetime import datetime

import httpx
from pydantic import ValidationError
from sqlalchemy import text

from ..artifacts.creator import create_crawl_artifact
from ..config import settings
from ..db import (
    new_id,
    tenant_scoped_qec_session,
    tenant_scoped_substrate_session,
    utc_now,
)
from ..db.models import QEHarnessRunRow
from ..service_token import mint_service_jwt
from ..substrate.schema import ExplorationBundle, RefusalError
from ..substrate.writer import write_exploration
from .rules import (
    BASELINE_RULE_ID,
    PII_SECRET_SENTINEL,
    UNREACHABLE_BASE_URL,
    VERDICT_CHAIN_ERROR,
    VERDICT_GREEN_WASH,
    VERDICT_PASS_BASELINE,
    WRITE_REQUIRE,
    RefuseRule,
    classify_verdict,
    load_fixture_bundle,
    select_rules,
)

logger = logging.getLogger(__name__)

# ONE version string per atomic write (§2.3); byte-identical to the
# explorations-router prefix so harness writes are indistinguishable rows.
_EXTRACTOR_VERSION_PREFIX = "qec_live_v1@"
_EXTRACTOR_VERSION_MAX = 50  # page_visits.extractor_version String(50)

_AUDIT_REPORT_FILENAME = "vkpower-audit-report.json"
# Grace between successive writes so created_at strictly orders versions
# (loader selection is MAX(created_at) — service.py:44-58).
_VERSION_ORDER_GRACE_SECONDS = 0.1


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        logger.warning("qec.harness.bad_env_value", extra={"env": name})
        return default


_HTTP_TIMEOUT = _env_float("QEC_HARNESS_HTTP_TIMEOUT_SECONDS", 300.0)
_POLL_INTERVAL = _env_float("QEC_HARNESS_POLL_INTERVAL_SECONDS", 5.0)
_POLL_TIMEOUT = _env_float("QEC_HARNESS_POLL_TIMEOUT_SECONDS", 600.0)


# ─── evidence helpers ────────────────────────────────────────────────────

def _json_safe(value, depth: int = 0):
    """Recursively coerce evidence into JSONB-serialisable primitives."""
    if depth > 12:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)[:500]


def _read_zip_evidence(content: bytes) -> tuple[list[str], dict | None]:
    """Extract file names + the HONEST-10 audit report from a delivered zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = zf.namelist()
            report = None
            if _AUDIT_REPORT_FILENAME in names:
                report = json.loads(zf.read(_AUDIT_REPORT_FILENAME).decode("utf-8"))
            return names[:40], report if isinstance(report, dict) else None
    except Exception as exc:
        logger.warning("qec.harness.zip_parse_failed", extra={"error": str(exc)[:200]})
        return [], None


async def _call(
    client: httpx.AsyncClient,
    chain: dict,
    step: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    expect_zip: bool = False,
) -> dict:
    """One evidenced HTTP interaction; transport failure sets chain_error.

    A connection failure NEVER reads as a refusal — classify_verdict maps
    ``chain_error`` to CHAIN_ERROR unless green-wash was already proven.
    """
    evidence: dict = {
        "step": step, "method": method, "path": path,
        "status": None, "json": None, "error": None,
    }
    chain[step] = evidence
    try:
        response = await client.request(
            method, path, json=json_body, params=params, headers=headers,
        )
        evidence["status"] = response.status_code
        content_type = (response.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            try:
                evidence["json"] = response.json()
            except ValueError:
                evidence["text_head"] = response.text[:500]
        elif expect_zip and response.status_code == 200:
            names, report = _read_zip_evidence(response.content)
            evidence["zip_files"] = names
            evidence["zip_bytes"] = len(response.content)
            evidence["audit_report"] = report
        else:
            evidence["text_head"] = response.text[:500]
    except httpx.HTTPError as exc:
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        chain["chain_error"] = (
            f"{step}: factory chain unreachable/broken — {evidence['error']}"
        )
        logger.error(
            "qec.harness.chain_http_error",
            extra={"step": step, "path": path, "error": evidence["error"]},
        )
    return evidence


def _extract_test_ids(test_cases_json: dict) -> list[str]:
    """Pull compiled-script test ids from the /test-cases listing
    (items[].test_case.test_id, falling back to items[].test_case_id)."""
    ids: list[str] = []
    for item in test_cases_json.get("items") or []:
        if not isinstance(item, dict):
            continue
        case = item.get("test_case")
        tid = ""
        if isinstance(case, dict):
            tid = str(case.get("test_id") or "")
        tid = tid or str(item.get("test_case_id") or "")
        if tid:
            ids.append(tid)
    return ids


# ─── substrate write + readback ──────────────────────────────────────────

def _extractor_version(crawl_id: str) -> str:
    version = f"{_EXTRACTOR_VERSION_PREFIX}{crawl_id}"
    if len(version) > _EXTRACTOR_VERSION_MAX:
        raise ValueError(
            f"harness crawl_id too long: extractor_version '{version}' exceeds "
            f"{_EXTRACTOR_VERSION_MAX} chars"
        )
    return version


async def _attempt_write(
    bundle_dict: dict,
    *,
    tenant_id: str,
    artifact_id: str,
    session_id: str,
) -> dict:
    """Try one atomic substrate write; return honest evidence, never raise.

    Outcome kinds: ok | refused (schema/writer evidence-rule refusal) |
    failed (unexpected error — infrastructure, not honesty).
    """
    crawl_id = str(bundle_dict.get("crawl_id") or "")
    try:
        version = _extractor_version(crawl_id)
    except ValueError as exc:
        return {"ok": False, "refused": False, "failed": True,
                "kind": "failed", "error": str(exc), "extractor_version": ""}
    try:
        bundle = ExplorationBundle.model_validate(bundle_dict)
    except ValidationError as exc:
        return {"ok": False, "refused": True, "failed": False,
                "kind": "schema_refusal", "error": str(exc)[:1000],
                "extractor_version": version}
    try:
        stats = await write_exploration(
            bundle,
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            session_id=session_id,
            extractor_version=version,
        )
        return {"ok": True, "refused": False, "failed": False, "kind": "ok",
                "error": None, "stats": stats.model_dump(),
                "extractor_version": version}
    except RefusalError as exc:
        return {"ok": False, "refused": True, "failed": False,
                "kind": "writer_refusal", "error": str(exc)[:1000],
                "extractor_version": version}
    except Exception as exc:
        logger.error(
            "qec.harness.write_failed",
            extra={"artifact_id": artifact_id, "error": str(exc)[:300]},
        )
        return {"ok": False, "refused": False, "failed": True, "kind": "failed",
                "error": str(exc)[:1000], "extractor_version": version}


async def _count_rows(
    tenant_id: str, sql: str, params: dict,
) -> int:
    async with tenant_scoped_substrate_session(tenant_id) as session:
        result = await session.execute(text(sql), params)
        return int(result.scalar_one())


async def _cross_tenant_readback(foreign_tenant: str, artifact_id: str) -> dict:
    """Row visibility for the artifact under a FOREIGN tenant's GUC — RLS
    must yield zero rows (the check runs real SQL, never trusts the API)."""
    try:
        rows = await _count_rows(
            foreign_tenant,
            "SELECT COUNT(*) FROM page_visits WHERE artifact_id = :a",
            {"a": artifact_id},
        )
        return {"tenant_id": foreign_tenant, "page_visit_rows": rows}
    except Exception as exc:
        return {"tenant_id": foreign_tenant, "page_visit_rows": None,
                "error": str(exc)[:300]}


_PII_READBACK_QUERIES: tuple[tuple[str, str, bool], ...] = (
    # (table, concat-of-value-bearing-columns, required)
    ("page_actions",
     "SELECT COALESCE(value, '') || ' ' || COALESCE(evidence_signals::text, '') "
     "FROM page_actions WHERE artifact_id = :a", True),
    ("page_visits",
     "SELECT COALESCE(form_snapshot::text, '') || ' ' || "
     "COALESCE(form_snapshot_signals::text, '') "
     "FROM page_visits WHERE artifact_id = :a", True),
    ("ground_truth_events",
     "SELECT COALESCE(value, '') || ' ' || COALESCE(signals::text, '') "
     "FROM ground_truth_events WHERE artifact_id = :a", False),
)


async def _pii_readback(tenant_id: str, artifact_id: str) -> dict:
    """Scan every value-bearing substrate column for the raw R6 sentinel.

    Fail-closed: if a REQUIRED table cannot be scanned, absence is
    unprovable → ``raw_secret_found`` is None (classifies CHAIN_ERROR),
    never a fabricated False.
    """
    found_in: list[str] = []
    scanned: dict[str, int] = {}
    errors: dict[str, str] = {}
    unprovable = False
    for table, sql, required in _PII_READBACK_QUERIES:
        try:
            async with tenant_scoped_substrate_session(tenant_id) as session:
                rows = (await session.execute(text(sql), {"a": artifact_id})).scalars().all()
            scanned[table] = len(rows)
            if any(PII_SECRET_SENTINEL in (row or "") for row in rows):
                found_in.append(table)
        except Exception as exc:
            message = str(exc)
            errors[table] = message[:300]
            missing = "does not exist" in message.lower()
            if required or not missing:
                unprovable = True
    raw_secret_found: bool | None
    if found_in:
        raw_secret_found = True
    elif unprovable:
        raw_secret_found = None
    else:
        raw_secret_found = False
    return {
        "raw_secret_found": raw_secret_found,
        "found_in": found_in,
        "rows_scanned": scanned,
        "table_errors": errors,
    }


async def _version_readback(
    tenant_id: str, artifact_id: str, versions: dict[str, str],
) -> dict:
    """Mirror the loader's latest-version selection (service.py:44-58) and
    count rows per known version — the R7 substrate truth."""
    out: dict = dict(versions)
    try:
        async with tenant_scoped_substrate_session(tenant_id) as session:
            current = (await session.execute(
                text("SELECT extractor_version FROM page_visits "
                     "WHERE artifact_id = :a ORDER BY created_at DESC LIMIT 1"),
                {"a": artifact_id},
            )).scalar_one_or_none()
        out["current_version"] = current or ""
        for label in ("old_version", "golden_version", "hijack_version"):
            version = versions.get(label) or ""
            out[f"{label.split('_', 1)[0]}_rows"] = (
                await _count_rows(
                    tenant_id,
                    "SELECT COUNT(*) FROM page_visits "
                    "WHERE artifact_id = :a AND extractor_version = :v",
                    {"a": artifact_id, "v": version},
                ) if version else 0
            )
    except Exception as exc:
        out["error"] = str(exc)[:300]
    return out


# ─── per-rule execution ──────────────────────────────────────────────────

def _fresh_bundle(fixture: dict) -> dict:
    """Deep-copy the golden fixture with a fresh crawl_id AND a fresh
    config_fingerprint, so every REFUSE-matrix rule gets its OWN artifact.

    ``compute_media_fingerprint`` (creator.py) deliberately EXCLUDES crawl_id
    and dedups on (target_url + config_fingerprint + explorer_version) — the
    correct production posture (an identical re-crawl maps to one artifact).
    But every harness rule replays the SAME golden fixture, so without a unique
    config_fingerprint they all dedup to ONE shared artifact and cross-
    contaminate (a valid baseline row leaks into a rule whose write should have
    been empty → false GREEN_WASH; and duplicate ground_truth_events ids →
    IntegrityError). Seeding config_fingerprint from the fresh crawl_id gives
    each rule an isolated artifact without weakening the real dedup.
    """
    bundle = copy.deepcopy(fixture)
    crawl_id = uuid.uuid4().hex[:24]
    bundle["crawl_id"] = crawl_id
    bundle["config_fingerprint"] = f"harness-{crawl_id}"
    return bundle


async def _create_artifact_for(
    bundle_dict: dict, *, tenant_id: str, rule_id: str, chain: dict,
) -> tuple[str, str]:
    """Create the fresh session+artifact rows; ('','') + chain_error on failure."""
    try:
        created = await create_crawl_artifact(
            tenant_id=tenant_id,
            target_url=str(bundle_dict.get("target_url") or ""),
            crawl_id=str(bundle_dict.get("crawl_id") or ""),
            config_fingerprint=str(bundle_dict.get("config_fingerprint") or ""),
            frame_count=int(bundle_dict.get("frame_count") or 0),
            meta={"harness_rule": rule_id, "harness": True},
        )
        chain["artifact"] = {
            "artifact_id": created.artifact_id, "session_id": created.session_id,
        }
        return created.artifact_id, created.session_id
    except Exception as exc:
        chain["chain_error"] = f"artifact creation failed: {str(exc)[:300]}"
        logger.error(
            "qec.harness.artifact_create_failed",
            extra={"rule_id": rule_id, "error": str(exc)[:300]},
        )
        return "", ""


def _apply_write_policy(rule: RefuseRule, write_evidence: dict, chain: dict) -> None:
    """Translate a write outcome into chain_error where the policy demands it."""
    if write_evidence.get("failed"):
        chain["chain_error"] = (
            f"substrate write failed (infrastructure): {write_evidence.get('error')}"
        )
        return
    if write_evidence.get("refused") and rule.write_policy == WRITE_REQUIRE:
        chain["chain_error"] = (
            "golden substrate write was refused — the fixture must satisfy "
            f"every evidence rule for {rule.rule_id}: {write_evidence.get('error')}"
        )


async def _drive_generate_and_ids(
    client: httpx.AsyncClient, chain: dict, artifact_id: str,
) -> list[str]:
    await _call(client, chain, "generate", "POST",
                f"/api/v1/test-factory/{artifact_id}/generate", json_body={})
    if chain.get("chain_error"):
        return []
    listing = await _call(
        client, chain, "test_cases", "GET",
        f"/api/v1/test-factory/{artifact_id}/test-cases",
        params={"status": "active", "limit": 200},
    )
    ids = _extract_test_ids(listing.get("json") or {})
    chain["test_cases"]["test_ids"] = ids
    return ids


async def _drive_rule_chain(
    rule: RefuseRule,
    chain: dict,
    *,
    client: httpx.AsyncClient,
    tenant_id: str,
    artifact_id: str,
) -> None:
    """Drive the factory interaction named by ``rule.chain_step``."""
    step = rule.chain_step

    if step in ("baseline_chain", "generate_playwright"):
        await _drive_generate_and_ids(client, chain, artifact_id)
        if not chain.get("chain_error"):
            await _call(client, chain, "playwright", "GET",
                        f"/api/v1/test-factory/{artifact_id}/playwright",
                        expect_zip=True)
        return

    if step == "verify_readiness":
        ids = await _drive_generate_and_ids(client, chain, artifact_id)
        if chain.get("chain_error"):
            return
        if not ids:
            chain["chain_error"] = "no test cases generated — cannot probe /verify"
            return
        await _call(
            client, chain, "verify", "POST",
            f"/api/v1/test-factory/{artifact_id}/scripts/{ids[0]}/verify",
            json_body={"base_url": rule.chain_overrides.get(
                "base_url", UNREACHABLE_BASE_URL)},
        )
        return

    if step == "semantic_check":
        ids = await _drive_generate_and_ids(client, chain, artifact_id)
        if chain.get("chain_error"):
            return
        if not ids:
            # A schema-level refusal upstream already proves honesty for R4;
            # otherwise the oracle cannot be probed — honest ambiguity.
            if not chain.get("write", {}).get("refused"):
                chain["chain_error"] = (
                    "no test cases generated — semantic oracle not probeable"
                )
            return
        await _call(
            client, chain, "semantic_check", "POST",
            f"/api/v1/test-factory/{artifact_id}/scenarios/{ids[0]}/semantic-check",
            params={"step_number": 1}, json_body={},
        )
        return

    if step == "cross_tenant_read":
        foreign_tenant = f"qec-hx-{uuid.uuid4().hex[:12]}"
        foreign_token = mint_service_jwt(foreign_tenant)
        await _call(
            client, chain, "cross_tenant_test_cases", "GET",
            f"/api/v1/test-factory/{artifact_id}/test-cases",
            params={"status": "active", "limit": 10},
            headers={"Authorization": f"Bearer {foreign_token}"},
        )
        chain["cross_tenant_readback"] = await _cross_tenant_readback(
            foreign_tenant, artifact_id,
        )
        return

    if step == "pii_readback":
        chain["pii_readback"] = await _pii_readback(tenant_id, artifact_id)
        return

    if step == "auto_heal_poll":
        ids = await _drive_generate_and_ids(client, chain, artifact_id)
        if chain.get("chain_error"):
            return
        if not ids:
            chain["chain_error"] = "no test cases generated — cannot start auto-heal"
            return
        submit = await _call(
            client, chain, "auto_heal_submit", "POST",
            f"/api/v1/test-factory/{artifact_id}/auto-heal/run-config",
            json_body={
                "test_ids": ids[:1],
                "base_url": rule.chain_overrides.get(
                    "base_url", UNREACHABLE_BASE_URL),
                "autonomous": True,
                "headed": False,
            },
        )
        run_id = str((submit.get("json") or {}).get("run_id") or "")
        if chain.get("chain_error"):
            return
        if not run_id:
            chain["chain_error"] = "auto-heal submit returned no run_id"
            return
        await _poll_run_to_terminal(client, chain, artifact_id, run_id)
        return

    chain["chain_error"] = f"unknown chain step: {step}"


async def _poll_run_to_terminal(
    client: httpx.AsyncClient, chain: dict, artifact_id: str, run_id: str,
) -> None:
    """Poll the run status route until a terminal state or the poll budget."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POLL_TIMEOUT
    polls = 0
    last: dict = {}
    while True:
        evidence = await _call(
            client, chain, "run_status_poll", "GET",
            f"/api/v1/test-factory/{artifact_id}/playwright/run/{run_id}",
        )
        polls += 1
        if chain.get("chain_error"):
            return
        last = evidence.get("json") or {}
        status = str(last.get("status") or "").lower()
        if status and status != "running":
            break
        if loop.time() >= deadline:
            chain["chain_error"] = (
                f"auto-heal run {run_id} still 'running' after "
                f"{_POLL_TIMEOUT:.0f}s poll budget — terminal honesty unprovable"
            )
            break
        await asyncio.sleep(_POLL_INTERVAL)
    chain["run_status_final"] = last
    chain["run_status_polls"] = polls


async def _execute_version_hijack(
    rule: RefuseRule, fixture: dict, *, client: httpx.AsyncClient, tenant_id: str,
) -> dict:
    """R7: old-version write → golden write → poisoned concurrent write that
    must die atomically; then loader-mirror readback + /generate."""
    chain: dict = {"rule_id": rule.rule_id, "chain_step": rule.chain_step}

    golden = _fresh_bundle(fixture)
    artifact_id, session_id = await _create_artifact_for(
        golden, tenant_id=tenant_id, rule_id=rule.rule_id, chain=chain,
    )
    if not artifact_id:
        return chain

    old_bundle = copy.deepcopy(golden)
    old_bundle["crawl_id"] = f"{golden['crawl_id'][:21]}old"
    old_write = await _attempt_write(
        old_bundle, tenant_id=tenant_id,
        artifact_id=artifact_id, session_id=session_id,
    )
    chain["write_old"] = old_write
    if not old_write.get("ok"):
        chain["chain_error"] = (
            f"old-version seed write did not succeed: {old_write.get('error')}"
        )
        return chain
    await asyncio.sleep(_VERSION_ORDER_GRACE_SECONDS)

    golden_write = await _attempt_write(
        golden, tenant_id=tenant_id,
        artifact_id=artifact_id, session_id=session_id,
    )
    chain["write"] = golden_write
    if not golden_write.get("ok"):
        chain["chain_error"] = (
            f"golden write did not succeed: {golden_write.get('error')}"
        )
        return chain
    await asyncio.sleep(_VERSION_ORDER_GRACE_SECONDS)

    hijack_bundle = rule.fixture_mutation(golden)
    hijack_write = await _attempt_write(
        hijack_bundle, tenant_id=tenant_id,
        artifact_id=artifact_id, session_id=session_id,
    )
    chain["write_hijack"] = hijack_write

    readback = await _version_readback(
        tenant_id, artifact_id, {
            "old_version": old_write.get("extractor_version", ""),
            "golden_version": golden_write.get("extractor_version", ""),
            "hijack_version": hijack_write.get("extractor_version", ""),
        },
    )
    readback["hijack_write_failed"] = not hijack_write.get("ok")
    chain["version_readback"] = readback

    await _call(client, chain, "generate", "POST",
                f"/api/v1/test-factory/{artifact_id}/generate", json_body={})
    return chain


async def _execute_rule(
    rule: RefuseRule, fixture: dict, *, client: httpx.AsyncClient, tenant_id: str,
) -> dict:
    """Run one rule end-to-end and return its full chain evidence."""
    if rule.chain_step == "version_hijack_readback":
        return await _execute_version_hijack(
            rule, fixture, client=client, tenant_id=tenant_id,
        )

    chain: dict = {"rule_id": rule.rule_id, "chain_step": rule.chain_step}
    mutated = rule.fixture_mutation(_fresh_bundle(fixture))

    artifact_id, session_id = await _create_artifact_for(
        mutated, tenant_id=tenant_id, rule_id=rule.rule_id, chain=chain,
    )
    if not artifact_id:
        return chain

    write_evidence = await _attempt_write(
        mutated, tenant_id=tenant_id,
        artifact_id=artifact_id, session_id=session_id,
    )
    chain["write"] = write_evidence
    _apply_write_policy(rule, write_evidence, chain)
    if chain.get("chain_error"):
        return chain

    await _drive_rule_chain(
        rule, chain, client=client, tenant_id=tenant_id, artifact_id=artifact_id,
    )
    return chain


# ─── matrix orchestration ────────────────────────────────────────────────

def _observed_summary(chain: dict) -> str:
    """Compact, human-scannable evidence digest for the qe_harness_runs row."""
    digest: dict = {}
    for key, value in chain.items():
        if isinstance(value, dict) and "status" in value:
            digest[key] = value.get("status")
    for key in ("write", "write_old", "write_hijack"):
        if key in chain:
            digest[key] = chain[key].get("kind")
    for key in ("pii_readback", "cross_tenant_readback",
                "version_readback", "run_status_final"):
        if key in chain:
            digest[key] = _json_safe(chain[key])
    if chain.get("chain_error"):
        digest["chain_error"] = chain["chain_error"]
    return json.dumps(digest, sort_keys=True, default=str)[:1800]


async def _persist_run(
    *, tenant_id: str, fixture_name: str, rule: RefuseRule,
    verdict: str, chain: dict,
) -> tuple[str, str]:
    """Persist one qe_harness_runs row; returns (harness_run_id, persist_error).

    Persistence failure is reported honestly on the verdict entry — the
    in-memory result still stands so the deploy gate never loses a verdict.
    """
    harness_run_id = new_id()
    report = {
        "chain": _json_safe(chain),
        "pins": list(rule.pins),
        "chain_step": rule.chain_step,
        "write_policy": rule.write_policy,
        "platform_api_url": settings.platform_api_url,
        "finished_at": utc_now().isoformat(),
    }
    try:
        async with tenant_scoped_qec_session(tenant_id) as session:
            session.add(QEHarnessRunRow(
                harness_run_id=harness_run_id,
                tenant_id=tenant_id,
                fixture_name=fixture_name,
                rule_id=rule.rule_id,
                expected=rule.expected,
                observed=_observed_summary(chain),
                verdict=verdict,
                report=report,
            ))
        return harness_run_id, ""
    except Exception as exc:
        logger.error(
            "qec.harness.persist_failed",
            extra={"rule_id": rule.rule_id, "tenant_id": tenant_id,
                   "error": str(exc)[:300]},
        )
        return "", str(exc)[:300]


async def run_refuse_matrix(
    *, tenant_id: str, fixture_name: str, rule_ids: list[str] | None,
) -> dict:
    """Drive the REFUSE matrix; persist per-rule evidence; aggregate honestly.

    Returns ``{"harness_run_ids": [...], "verdicts": [...],
    "green_wash_detected": bool}`` (router contract).  One rule's failure
    never aborts the matrix — every rule always yields a persisted verdict.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required to run the harness")
    fixture = load_fixture_bundle(fixture_name)
    rules = select_rules(rule_ids)

    token = mint_service_jwt(tenant_id)
    harness_run_ids: list[str] = []
    verdict_entries: list[dict] = []

    async with httpx.AsyncClient(
        base_url=settings.platform_api_url,
        timeout=httpx.Timeout(_HTTP_TIMEOUT, connect=15.0),
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    ) as client:
        for rule in rules:
            logger.info(
                "qec.harness.rule_started",
                extra={"rule_id": rule.rule_id, "fixture": fixture_name,
                       "tenant_id": tenant_id},
            )
            try:
                chain = await _execute_rule(
                    rule, fixture, client=client, tenant_id=tenant_id,
                )
            except Exception as exc:  # a crashed rule is CHAIN_ERROR, never skipped
                logger.error(
                    "qec.harness.rule_crashed",
                    extra={"rule_id": rule.rule_id, "error": str(exc)[:300]},
                )
                chain = {
                    "rule_id": rule.rule_id,
                    "chain_step": rule.chain_step,
                    "chain_error": f"rule execution crashed: {str(exc)[:500]}",
                }
            verdict = classify_verdict(rule, chain)
            harness_run_id, persist_error = await _persist_run(
                tenant_id=tenant_id, fixture_name=fixture_name,
                rule=rule, verdict=verdict, chain=chain,
            )
            if harness_run_id:
                harness_run_ids.append(harness_run_id)
            entry = {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "verdict": verdict,
                "harness_run_id": harness_run_id,
                "expected": rule.expected,
                "observed": _observed_summary(chain),
            }
            if persist_error:
                entry["persist_error"] = persist_error
            verdict_entries.append(entry)
            log = logger.error if verdict == VERDICT_GREEN_WASH else logger.info
            log(
                "qec.harness.rule_finished",
                extra={"rule_id": rule.rule_id, "verdict": verdict,
                       "harness_run_id": harness_run_id},
            )

    return {
        "harness_run_ids": harness_run_ids,
        "verdicts": verdict_entries,
        "green_wash_detected": any(
            v["verdict"] == VERDICT_GREEN_WASH for v in verdict_entries
        ),
    }


# ─── CLI deploy gate (python -m app.harness.runner) ──────────────────────

def _exit_code(result: dict, ran_full_matrix: bool) -> int:
    """2 = green-wash (pinned non-zero contract); 1 = honesty unprovable;
    0 = baseline passed + every rule refused correctly."""
    if result.get("green_wash_detected"):
        return 2
    verdicts = {v["rule_id"]: v["verdict"] for v in result.get("verdicts", [])}
    if any(v == VERDICT_CHAIN_ERROR for v in verdicts.values()):
        return 1
    if ran_full_matrix and verdicts.get(BASELINE_RULE_ID) != VERDICT_PASS_BASELINE:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the REFUSE matrix as a process — the CI/deploy honesty gate."""
    parser = argparse.ArgumentParser(
        prog="python -m app.harness.runner",
        description=("QE-Central Phase-0 REFUSE harness. Exit 2 on any "
                     "GREEN_WASH_DETECTED, 1 when honesty is unprovable, "
                     "0 only on a fully honest chain."),
    )
    parser.add_argument(
        "--tenant",
        default=os.getenv("QEC_HARNESS_TENANT_ID", "qec-harness-tenant"),
        help="tenant to run under (default env QEC_HARNESS_TENANT_ID)",
    )
    parser.add_argument(
        "--fixture", default="golden_3page_form",
        help="fixture bundle name in app/harness/fixtures/",
    )
    parser.add_argument(
        "--rules", default="",
        help="comma-separated subset (R1..R8); empty = baseline + full matrix",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, settings.qec_log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )

    if not settings.qe_harness_enabled:
        print(
            "REFUSE harness is disabled (QE_HARNESS_ENABLED unset) — it "
            "creates real artifacts and drives real factory endpoints; "
            "enable it explicitly to run the gate.",
            file=sys.stderr,
        )
        return 3

    rule_ids = [r.strip() for r in args.rules.split(",") if r.strip()] or None
    try:
        result = asyncio.run(run_refuse_matrix(
            tenant_id=args.tenant, fixture_name=args.fixture, rule_ids=rule_ids,
        ))
    except Exception as exc:
        print(f"harness run failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    code = _exit_code(result, ran_full_matrix=rule_ids is None)
    if code == 2:
        print("DEPLOY GATE: GREEN_WASH_DETECTED — failing the build.",
              file=sys.stderr)
    elif code == 1:
        print("DEPLOY GATE: honesty unprovable (chain errors / failed "
              "baseline) — failing the build.", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
