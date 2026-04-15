#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════╗
║  Nexus QA — Real End-to-End Production Demo                  ║
║                                                              ║
║  This script runs a REAL E2E demo through the entire Nexus   ║
║  QA platform — no mocks, no stubs, real AI inference.        ║
║                                                              ║
║  Prerequisites:                                              ║
║    1. Docker running with:                                   ║
║       docker compose -f docker-compose.dev.yml up -d         ║
║    2. Ollama models pulled:                                  ║
║       python scripts/setup_ollama.py                         ║
║    3. All services running (any method):                     ║
║       python scripts/start_all_services.py                   ║
║       OR                                                     ║
║       docker compose -f infrastructure/docker/               ║
║         docker-compose.yml up -d                             ║
║                                                              ║
║  Usage:                                                      ║
║    python scripts/demo_e2e.py                                ║
║    python scripts/demo_e2e.py --demo quick                   ║
║    python scripts/demo_e2e.py --demo full                    ║
║    python scripts/demo_e2e.py --demo orchestrator            ║
║    python scripts/demo_e2e.py --gateway http://localhost:8080║
╚═══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import textwrap
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx required.  pip install httpx")
    sys.exit(1)

# ── Defaults ──────────────────────────────────────────────────
GATEWAY = os.environ.get("NEXUS_GATEWAY_URL", "http://localhost:8080")
AUTH_URL = os.environ.get("AUTH_URL", "http://localhost:8000")
PLATFORM_API = os.environ.get("PLATFORM_API_URL", "http://localhost:8091")
ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:8100")
DEMO_TENANT = "nexus-demo"
DEMO_EMAIL = "admin@nexus.local"
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "change-this-password")

# ── Colour helpers ────────────────────────────────────────────
NO_COLOUR = os.environ.get("NO_COLOR") or os.environ.get("CI")

def _c(code: str, text: str) -> str:
    if NO_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"

def green(t: str) -> str: return _c("32", t)
def red(t: str) -> str: return _c("31", t)
def yellow(t: str) -> str: return _c("33", t)
def cyan(t: str) -> str: return _c("36", t)
def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)

# ── Logging ───────────────────────────────────────────────────
class DemoLog:
    def __init__(self):
        self.steps: list[dict] = []
        self.start_time = time.time()

    def step(self, name: str, status: str, detail: str = "", duration_ms: float = 0):
        self.steps.append({
            "name": name, "status": status,
            "detail": detail, "duration_ms": round(duration_ms, 1)
        })
        icon = {"PASS": green("✓"), "FAIL": red("✗"), "SKIP": yellow("○"), "INFO": cyan("ℹ")}
        ts = f"[{time.time() - self.start_time:6.1f}s]"
        dur = f" ({duration_ms:.0f}ms)" if duration_ms else ""
        print(f"  {icon.get(status, '?')} {dim(ts)} {name}{dur}")
        if detail:
            for line in detail.split("\n"):
                print(f"       {dim(line)}")

    def summary(self):
        passed = sum(1 for s in self.steps if s["status"] == "PASS")
        failed = sum(1 for s in self.steps if s["status"] == "FAIL")
        skipped = sum(1 for s in self.steps if s["status"] == "SKIP")
        total_time = time.time() - self.start_time
        print(f"\n{'=' * 60}")
        print(bold(f"  Demo Summary: {passed} passed, {failed} failed, {skipped} skipped"))
        print(f"  Total time: {total_time:.1f}s")
        print(f"{'=' * 60}\n")
        return failed == 0

log = DemoLog()

# ═══════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════

def api(method: str, url: str, token: str | None = None,
        json_body: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        timeout: float = 120.0) -> httpx.Response:
    """Make an API call with optional auth."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=timeout) as client:
        return client.request(
            method, url, headers=headers,
            json=json_body, data=data, files=files,
        )


def poll_job(base_url: str, job_id: str, token: str,
             max_wait: int = 300, interval: int = 3) -> dict:
    """Poll an async job until completion."""
    for _ in range(max_wait // interval):
        resp = api("GET", f"{base_url}/jobs/{job_id}", token=token)
        if resp.status_code != 200:
            return {"status": "error", "error": f"HTTP {resp.status_code}"}
        body = resp.json()
        status = body.get("status", "unknown")
        if status in ("completed", "complete", "done"):
            return body
        if status in ("failed", "error"):
            return body
        time.sleep(interval)
    return {"status": "timeout"}


def section(title: str):
    """Print a section header."""
    print(f"\n{'─' * 60}")
    print(bold(f"  {title}"))
    print(f"{'─' * 60}")


# ═══════════════════════════════════════════════════════════════
# Sample Demo Data (Real, not mock)
# ═══════════════════════════════════════════════════════════════

SAMPLE_TRANSCRIPT = textwrap.dedent("""\
    SME: Welcome, today I'll walk you through our online pharmacy ordering system.
    The process starts when a patient searches for their prescribed medication.

    Analyst: How does the search work?

    SME: The patient types the medication name — for example, Amoxicillin 500mg.
    The system performs a fuzzy search against our FDA-approved drug database.
    If the medication requires a prescription, the system checks if we have a
    valid e-prescription on file from their doctor, Dr. Sarah Johnson at MedClinic.

    Analyst: What if there's no prescription on file?

    SME: Good question. If no active prescription exists, the system blocks the order
    and shows a message: "Prescription required. Please contact your healthcare
    provider." The patient cannot bypass this — it's a regulatory requirement
    under DEA Schedule rules. Their social security number 123-45-6789 is never
    shown on screen.

    Analyst: What about insurance?

    SME: After prescription verification, the system calls the insurance eligibility
    API with the patient's member ID. The copay is calculated based on formulary
    tier. If the patient's insurance — say, Blue Cross policy BC-2024-78901 — covers
    the medication, we show the copay amount. If not covered, we show the cash price
    with a link to manufacturer discount programs.

    Analyst: And the checkout process?

    SME: The patient reviews their order, confirms the shipping address
    (123 Main Street, Apt 4B, Springfield, IL 62704), selects standard or
    express delivery, and enters payment. Credit card numbers like
    4532-1234-5678-9012 are tokenized immediately — we never store raw card data.
    The order is placed, and the patient receives a confirmation email at
    john.doe@email.com with estimated delivery date.

    SME: One important business rule — if the medication is a controlled substance
    (Schedule II-V), we require identity verification before shipping. The
    patient must upload a government-issued ID. We verify it against their
    profile: John Michael Doe, DOB 03/15/1985, phone (555) 123-4567.

    Analyst: What about refills?

    SME: Refills are simpler. If the prescription allows refills and hasn't expired,
    the patient clicks "Reorder" and the system auto-fills everything. We check
    the refill count — if it's the last allowed refill, we notify both the
    patient and the prescribing physician. The system auto-generates a refill
    request to the doctor 7 days before the current supply runs out.
""")

SAMPLE_DOCUMENT = textwrap.dedent("""\
    BUSINESS REQUIREMENTS DOCUMENT
    Project: Online Pharmacy Platform v2.1
    Date: 2024-12-15

    1. MEDICATION SEARCH
       BR-PHARM-001: The system SHALL support fuzzy search across the FDA drug database
       with a response time under 200ms for 95th percentile.

       BR-PHARM-002: Search results SHALL display drug name, dosage forms, manufacturer,
       and pricing (insurance copay and cash price).

    2. PRESCRIPTION VALIDATION
       BR-PHARM-010: Prescription medications SHALL NOT be dispensed without a valid
       e-prescription from a licensed healthcare provider.

       BR-PHARM-011: E-prescriptions SHALL be validated against the EPCS
       (Electronic Prescriptions for Controlled Substances) standard for
       Schedule II-V medications.

       BR-PHARM-012: Expired prescriptions (older than 12 months) SHALL be rejected
       with a patient notification to contact their provider.

    3. INSURANCE PROCESSING
       BR-PHARM-020: Insurance eligibility SHALL be verified in real-time via the
       payer's eligibility API before displaying copay amounts.

       BR-PHARM-021: If insurance does not cover the medication, the system SHALL
       display the cash price and link to manufacturer assistance programs.

    4. ORDER PROCESSING
       BR-PHARM-030: All credit card data SHALL be tokenized at the point of entry.
       Raw card numbers SHALL NOT be stored in any system database.

       BR-PHARM-031: Controlled substance orders (Schedule II-V) SHALL require
       identity verification via government-issued photo ID.

    5. REFILL MANAGEMENT
       BR-PHARM-040: The system SHALL auto-generate refill reminders 7 days before
       the estimated supply depletion date.

       BR-PHARM-041: Last-refill notifications SHALL be sent to both the patient
       and the prescribing physician.
""")


# ═══════════════════════════════════════════════════════════════
# Demo Scenarios
# ═══════════════════════════════════════════════════════════════

def demo_health_check() -> bool:
    """Phase 0: Verify all services are alive."""
    section("Phase 0 — Service Health Check")
    services = [
        ("Auth Service",    f"{AUTH_URL}/health"),
        ("Gateway",         f"{GATEWAY}/health"),
        ("Shield Engine",   "http://localhost:8001/health"),
        ("Ears Engine",     "http://localhost:8002/health"),
        ("Eyes Engine",     "http://localhost:8003/health"),
        ("Heart Engine",    "http://localhost:8004/health"),
        ("Backbone Engine", "http://localhost:8005/health"),
        ("Nerves Engine",   "http://localhost:8006/health"),
        ("Legs Engine",     "http://localhost:8007/health"),
        ("Hands Engine",    "http://localhost:8008/health"),
        ("Spine Engine",    "http://localhost:8009/health"),
        ("Mouth Engine",    "http://localhost:8010/health"),
        ("Brain Engine",    "http://localhost:8011/health"),
        ("Platform API",    f"{PLATFORM_API}/health"),
        ("Orchestrator",    f"{ORCHESTRATOR_URL}/health"),
    ]
    all_up = True
    for name, url in services:
        t0 = time.time()
        try:
            r = api("GET", url, timeout=5)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                log.step(f"Health: {name}", "PASS", duration_ms=ms)
            else:
                log.step(f"Health: {name}", "FAIL", f"HTTP {r.status_code}")
                all_up = False
        except Exception as e:
            log.step(f"Health: {name}", "FAIL", str(e))
            all_up = False
    return all_up


def demo_authenticate() -> str | None:
    """Phase 1: Authenticate and get JWT token."""
    section("Phase 1 — Authentication")
    t0 = time.time()

    # Try to create tenant first (idempotent)
    try:
        # Login as admin first
        r = api("POST", f"{AUTH_URL}/api/v1/auth/login", json_body={
            "email": DEMO_EMAIL, "password": DEMO_PASSWORD,
        })
        if r.status_code == 200:
            token = r.json()["access_token"]
            user = r.json().get("user", {})
            ms = (time.time() - t0) * 1000
            log.step("Login", "PASS",
                     f"User: {user.get('email', 'N/A')} | Role: {user.get('role', 'N/A')} | Tenant: {user.get('tenant_id', 'N/A')}",
                     duration_ms=ms)
            return token
        else:
            log.step("Login", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        log.step("Login", "FAIL", str(e))
        return None


def demo_shield(token: str) -> dict:
    """Phase 2: PII Detection & Redaction (Shield Engine)."""
    section("Phase 2 — PII Detection & Redaction (Shield Engine)")
    result = {"redacted_text": SAMPLE_TRANSCRIPT, "entities": []}

    # 2a. Analyze for PII
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8001/api/v1/shield/analyze",
                token=token, json_body={
                    "text": SAMPLE_TRANSCRIPT,
                    "tenant_id": DEMO_TENANT,
                })
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            entities = body.get("entities", [])
            risk = body.get("risk_level", "unknown")
            log.step("PII Analysis", "PASS",
                     f"Found {len(entities)} PII entities | Risk: {risk}",
                     duration_ms=ms)
            for ent in entities[:5]:
                etype = ent.get("entity_type", ent.get("type", "?"))
                etext = ent.get("text", ent.get("value", "?"))[:30]
                log.step(f"  → {etype}", "INFO", f'"{etext}..."')
            result["entities"] = entities
        else:
            log.step("PII Analysis", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("PII Analysis", "FAIL", str(e))

    # 2b. Redact PII
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8001/api/v1/shield/redact",
                token=token, json_body={
                    "text": SAMPLE_TRANSCRIPT,
                    "tenant_id": DEMO_TENANT,
                })
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            safe_text = body.get("safe_text", "")
            mapping_id = body.get("mapping_id", "")
            entity_count = body.get("entity_count", 0)
            log.step("PII Redaction", "PASS",
                     f"Redacted {entity_count} entities | Mapping: {mapping_id[:12]}...",
                     duration_ms=ms)
            # Show a snippet of redacted text
            snippet = safe_text[:200].replace("\n", " ")
            log.step("  Redacted preview", "INFO", f'"{snippet}..."')
            result["redacted_text"] = safe_text
            result["mapping_id"] = mapping_id
        else:
            log.step("PII Redaction", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("PII Redaction", "FAIL", str(e))

    return result


def demo_heart(token: str, transcript: str) -> dict:
    """Phase 3: AI Rule Extraction & Test Generation (Heart Engine)."""
    section("Phase 3 — AI Rule Extraction & Test Generation (Heart Engine)")
    result = {"rules": [], "test_cases": []}

    # 3a. Extract business rules from transcript
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8004/api/v1/heart/extract-rules",
                token=token, json_body={
                    "transcript": transcript,
                    "session_id": f"demo-{uuid.uuid4().hex[:8]}",
                    "tenant_id": DEMO_TENANT,
                }, timeout=180)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            rules = body.get("rules", [])
            edge_cases = body.get("edge_cases", [])
            questions = body.get("questions_for_sme", [])
            log.step("Rule Extraction", "PASS",
                     f"{len(rules)} rules | {len(edge_cases)} edge cases | {len(questions)} SME questions",
                     duration_ms=ms)
            for rule in rules[:5]:
                rule_id = rule.get("rule_id", rule.get("id", "?"))
                desc = rule.get("description", rule.get("text", "?"))[:80]
                log.step(f"  → {rule_id}", "INFO", desc)
            result["rules"] = rules
        else:
            log.step("Rule Extraction", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.step("Rule Extraction", "FAIL", str(e))

    # 3b. Analyze business document
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8004/api/v1/heart/analyze",
                token=token, json_body={
                    "content": SAMPLE_DOCUMENT,
                    "tenant_id": DEMO_TENANT,
                    "document_type": "brd",
                }, timeout=180)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            rules_found = body.get("rules_found", 0)
            risks = body.get("risks", [])
            log.step("Document Analysis", "PASS",
                     f"Rules found: {rules_found} | Risks: {len(risks)}",
                     duration_ms=ms)
        else:
            log.step("Document Analysis", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("Document Analysis", "FAIL", str(e))

    # 3c. Generate test cases
    if result["rules"]:
        t0 = time.time()
        try:
            r = api("POST", "http://localhost:8004/api/v1/heart/generate-tests",
                    token=token, json_body={
                        "rules": result["rules"],
                        "tenant_id": DEMO_TENANT,
                        "coverage_targets": ["happy_path", "boundary", "negative", "edge_case"],
                    }, timeout=180)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                test_cases = body.get("test_cases", [])
                coverage = body.get("coverage_summary", {})
                log.step("Test Generation", "PASS",
                         f"{len(test_cases)} test cases generated",
                         duration_ms=ms)
                if coverage:
                    log.step("  Coverage", "INFO",
                             f"Happy: {coverage.get('happy_path', '?')} | "
                             f"Boundary: {coverage.get('boundary', '?')} | "
                             f"Negative: {coverage.get('negative', '?')} | "
                             f"Edge: {coverage.get('edge_case', '?')}")
                for tc in test_cases[:3]:
                    title = tc.get("title", tc.get("name", "?"))[:60]
                    tc_type = tc.get("test_type", tc.get("type", "?"))
                    log.step(f"  → [{tc_type}]", "INFO", title)
                result["test_cases"] = test_cases
            else:
                log.step("Test Generation", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log.step("Test Generation", "FAIL", str(e))
    else:
        log.step("Test Generation", "SKIP", "No rules extracted - using fallback")

    # 3d. Ask a question
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8004/api/v1/heart/ask",
                token=token, json_body={
                    "question": "What happens when a patient tries to order a controlled substance without a valid prescription?",
                    "tenant_id": DEMO_TENANT,
                    "context": transcript[:2000],
                }, timeout=120)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            answer = body.get("answer", "")[:200]
            confidence = body.get("confidence", "?")
            log.step("AI Q&A", "PASS",
                     f"Confidence: {confidence}\n{answer}",
                     duration_ms=ms)
        else:
            log.step("AI Q&A", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("AI Q&A", "FAIL", str(e))

    return result


def demo_hands(token: str) -> dict:
    """Phase 4: Synthetic Test Data Generation (Hands Engine)."""
    section("Phase 4 — Synthetic Test Data (Hands Engine)")
    result = {"profiles": []}

    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8008/api/v1/hands/generate-profiles",
                token=token, json_body={
                    "tenant_id": DEMO_TENANT,
                    "count": 10,
                    "schema": {
                        "first_name": "string",
                        "last_name": "string",
                        "email": "email",
                        "date_of_birth": "date",
                        "phone": "phone",
                        "address": "address",
                        "insurance_id": "alphanumeric:12",
                        "medication": "choice:Amoxicillin 500mg,Lisinopril 10mg,Metformin 1000mg,Atorvastatin 20mg",
                    },
                }, timeout=30)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            profiles = body.get("profiles", body.get("data", []))
            log.step("Generate Profiles", "PASS",
                     f"{len(profiles)} synthetic patient profiles created",
                     duration_ms=ms)
            if profiles:
                sample = profiles[0]
                log.step("  Sample", "INFO",
                         f"{sample.get('first_name', '?')} {sample.get('last_name', '?')} | "
                         f"{sample.get('email', '?')} | {sample.get('medication', '?')}")
            result["profiles"] = profiles
        else:
            log.step("Generate Profiles", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.step("Generate Profiles", "FAIL", str(e))

    return result


def demo_backbone(token: str, rules: list) -> dict:
    """Phase 5: Knowledge Graph Storage (Backbone Engine)."""
    section("Phase 5 — Knowledge Graph (Backbone Engine)")
    result = {"stored": 0}

    if not rules:
        log.step("Store Rules", "SKIP", "No rules to store")
        return result

    # Store rules in knowledge graph
    stored = 0
    for rule in rules[:5]:  # Store up to 5 rules for demo
        t0 = time.time()
        try:
            r = api("POST", "http://localhost:8005/api/v1/backbone/rules",
                    token=token, json_body={
                        "tenant_id": DEMO_TENANT,
                        "rule": rule,
                    }, timeout=30)
            ms = (time.time() - t0) * 1000
            if r.status_code in (200, 201):
                stored += 1
                rule_id = rule.get("rule_id", rule.get("id", "?"))
                log.step(f"Stored: {rule_id}", "PASS", duration_ms=ms)
            else:
                log.step("Store Rule", "FAIL", f"HTTP {r.status_code}")
        except Exception as e:
            log.step("Store Rule", "FAIL", str(e))

    result["stored"] = stored

    # Search knowledge graph
    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8005/api/v1/backbone/search",
                token=token, json_body={
                    "tenant_id": DEMO_TENANT,
                    "query": "prescription validation requirements",
                    "top_k": 5,
                }, timeout=30)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            results_list = body.get("results", [])
            log.step("Semantic Search", "PASS",
                     f"{len(results_list)} relevant results found",
                     duration_ms=ms)
        else:
            log.step("Semantic Search", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("Semantic Search", "FAIL", str(e))

    return result


def demo_mouth(token: str, rules: list, test_cases: list) -> dict:
    """Phase 6: Report Generation (Mouth Engine)."""
    section("Phase 6 — Report Generation (Mouth Engine)")
    result = {"report_id": None}

    t0 = time.time()
    try:
        r = api("POST", "http://localhost:8010/api/v1/mouth/generate",
                token=token, json_body={
                    "tenant_id": DEMO_TENANT,
                    "session_id": f"demo-{uuid.uuid4().hex[:8]}",
                    "report_type": "executive_summary",
                    "format": "html",
                    "title": "Nexus QA Demo — Online Pharmacy Platform Analysis",
                    "description": "Real-time AI-generated executive summary from SME knowledge capture session",
                    "rules": rules[:10],
                    "test_cases": test_cases[:10],
                    "include_recommendations": True,
                }, timeout=60)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            report_id = body.get("report_id", "?")
            log.step("Generate Report", "PASS",
                     f"Report ID: {report_id} | Type: executive_summary",
                     duration_ms=ms)
            result["report_id"] = report_id

            # Poll for report completion
            time.sleep(2)
            r2 = api("GET", f"http://localhost:8010/api/v1/mouth/reports/{report_id}",
                     token=token)
            if r2.status_code == 200:
                meta = r2.json()
                ready = meta.get("ready", False)
                log.step("Report Status", "PASS" if ready else "INFO",
                         f"Ready: {ready}")

                if ready:
                    # Fetch report content
                    r3 = api("GET", f"http://localhost:8010/api/v1/mouth/reports/{report_id}/data",
                             token=token)
                    if r3.status_code == 200:
                        data = r3.json()
                        log.step("Report Content", "PASS", "Report data retrieved successfully")
        else:
            log.step("Generate Report", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.step("Generate Report", "FAIL", str(e))

    # Report engine stats
    t0 = time.time()
    try:
        r = api("GET", "http://localhost:8010/api/v1/mouth/stats", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            total = body.get("total_reports", 0)
            caps = body.get("capabilities", [])
            formats = body.get("supported_formats", [])
            log.step("Engine Stats", "PASS",
                     f"Total reports: {total} | Formats: {formats} | Capabilities: {len(caps)}",
                     duration_ms=ms)
    except Exception as e:
        log.step("Engine Stats", "FAIL", str(e))

    return result


def demo_platform_api(token: str) -> dict:
    """Phase 7: Platform API — Test Case Management."""
    section("Phase 7 — Platform API (Test Case CRUD)")
    result = {"test_case_id": None}

    # Create test case
    t0 = time.time()
    try:
        payload = {
            "tenant_id": DEMO_TENANT,
            "title": "Pharmacy Prescription Validation — E2E Test",
            "description": "Validates that the pharmacy system correctly enforces prescription requirements for controlled substances",
            "test_type": "e2e",
            "priority": "critical",
            "version": 1,
            "target_systems": ["web", "api"],
            "validates_rules": ["BR-PHARM-010", "BR-PHARM-011", "BR-PHARM-031"],
            "tags": ["pharmacy", "controlled-substance", "prescription", "compliance"],
            "steps": [
                {"step_number": 1, "action": "Navigate to pharmacy portal", "expected_result": "Home page loads with search bar"},
                {"step_number": 2, "action": "Search for 'Oxycodone 5mg' (Schedule II)", "expected_result": "Drug found with prescription-required badge"},
                {"step_number": 3, "action": "Click 'Add to Cart' without prescription", "expected_result": "System blocks: 'Valid prescription required'"},
                {"step_number": 4, "action": "Upload valid e-prescription", "expected_result": "Prescription validated, drug added to cart"},
                {"step_number": 5, "action": "Proceed to checkout", "expected_result": "Identity verification prompt displayed"},
                {"step_number": 6, "action": "Upload government ID", "expected_result": "ID verified, checkout enabled"},
                {"step_number": 7, "action": "Complete order with payment", "expected_result": "Order confirmed, confirmation email sent"},
            ],
            "preconditions": [
                {"description": "Patient account exists with verified insurance"},
                {"description": "E-prescription from licensed provider on file"},
                {"description": "Payment gateway in sandbox mode"},
            ],
            "data_workbook": [
                {"field_name": "PatientName", "field_value": "Sarah Connor", "field_type": "string"},
                {"field_name": "Medication", "field_value": "Oxycodone 5mg", "field_type": "string"},
                {"field_name": "ScheduleClass", "field_value": "II", "field_type": "string"},
                {"field_name": "InsuranceID", "field_value": "BC-2024-DEMO-001", "field_type": "string"},
            ],
        }
        r = api("POST", f"{PLATFORM_API}/api/v1/test-cases",
                token=token, json_body=payload, timeout=15)
        ms = (time.time() - t0) * 1000
        if r.status_code == 201:
            body = r.json()
            tc_id = body.get("test_case_id", "?")
            steps = body.get("steps", 0)
            data = body.get("data_workbook_entries", 0)
            log.step("Create Test Case", "PASS",
                     f"ID: {tc_id} | Steps: {steps} | Data: {data}",
                     duration_ms=ms)
            result["test_case_id"] = tc_id
        else:
            log.step("Create Test Case", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.step("Create Test Case", "FAIL", str(e))

    # List test cases
    if result["test_case_id"]:
        t0 = time.time()
        try:
            r = api("GET", f"{PLATFORM_API}/api/v1/test-cases",
                    token=token, json_body=None)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                total = body.get("total", 0)
                log.step("List Test Cases", "PASS",
                         f"Total: {total} test cases in system",
                         duration_ms=ms)
            else:
                log.step("List Test Cases", "FAIL", f"HTTP {r.status_code}")
        except Exception as e:
            log.step("List Test Cases", "FAIL", str(e))

        # Get stats
        t0 = time.time()
        try:
            r = api("GET", f"{PLATFORM_API}/api/v1/test-cases/stats",
                    token=token, json_body=None)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                log.step("Test Case Stats", "PASS",
                         f"Total: {body.get('total_test_cases', 0)} | "
                         f"Steps: {body.get('total_steps', 0)} | "
                         f"Data: {body.get('total_data_fields', 0)}",
                         duration_ms=ms)
        except Exception as e:
            log.step("Test Case Stats", "FAIL", str(e))

        # Export
        t0 = time.time()
        try:
            r = api("POST", f"{PLATFORM_API}/api/v1/test-cases/export",
                    token=token, json_body={
                        "tenant_id": DEMO_TENANT,
                        "format": "json",
                    }, timeout=30)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                log.step("Export Test Cases", "PASS",
                         f"Format: {body.get('format', '?')} | "
                         f"Records: {body.get('record_count', 0)} | "
                         f"Size: {body.get('file_size_bytes', 0)}B",
                         duration_ms=ms)
        except Exception as e:
            log.step("Export Test Cases", "FAIL", str(e))

        # Cleanup - delete test case
        t0 = time.time()
        try:
            r = api("DELETE", f"{PLATFORM_API}/api/v1/test-cases/{result['test_case_id']}",
                    token=token, timeout=10)
            ms = (time.time() - t0) * 1000
            if r.status_code == 204:
                log.step("Cleanup Test Case", "PASS", duration_ms=ms)
            else:
                log.step("Cleanup Test Case", "INFO", f"HTTP {r.status_code}")
        except Exception as e:
            log.step("Cleanup Test Case", "INFO", str(e))

    return result


def demo_orchestrator(token: str) -> dict:
    """Phase 8: Orchestrator — Full Workflow Execution."""
    section("Phase 8 — Orchestrator (Full AI Pipeline)")
    result = {"workflow_id": None}

    # List available chains
    t0 = time.time()
    try:
        r = api("GET", f"{ORCHESTRATOR_URL}/api/v1/orchestrator/chains", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            chains = r.json()
            if isinstance(chains, list):
                log.step("Available Chains", "PASS",
                         f"{len(chains)} chains registered",
                         duration_ms=ms)
                for chain in chains:
                    name = chain.get("name", "?")
                    stages = chain.get("stage_count", "?")
                    cid = chain.get("chain_id", "?")
                    log.step(f"  → {cid}", "INFO", f"{name} ({stages} stages)")
        else:
            log.step("Available Chains", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        log.step("Available Chains", "FAIL", str(e))

    # Start a knowledge-capture workflow (lighter than full QA pipeline)
    t0 = time.time()
    try:
        r = api("POST", f"{ORCHESTRATOR_URL}/api/v1/orchestrator/workflows/start",
                token=token, json_body={
                    "chain_id": "nexus.knowledge-capture",
                    "tenant_id": DEMO_TENANT,
                    "input_data": {
                        "session_name": "Pharmacy Knowledge Capture Demo",
                        "transcript": SAMPLE_TRANSCRIPT[:2000],
                    },
                }, timeout=30)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            wf_id = body.get("workflow_id", "?")
            status = body.get("status", "?")
            log.step("Start Workflow", "PASS",
                     f"Workflow: {wf_id} | Status: {status}",
                     duration_ms=ms)
            result["workflow_id"] = wf_id

            # Poll for progress
            for i in range(10):
                time.sleep(5)
                r2 = api("GET", f"{ORCHESTRATOR_URL}/api/v1/orchestrator/workflows/{wf_id}",
                         token=token)
                if r2.status_code == 200:
                    wf = r2.json()
                    wf_status = wf.get("status", "?")
                    completed = wf.get("stages_completed", 0)
                    total = wf.get("stages_total", 0)
                    log.step(f"  Poll #{i+1}", "INFO",
                             f"Status: {wf_status} | Progress: {completed}/{total}")
                    if wf_status in ("completed", "failed", "cancelled"):
                        if wf_status == "completed":
                            log.step("Workflow Complete", "PASS",
                                     f"All {total} stages finished successfully")
                        else:
                            error = wf.get("error", "")
                            log.step("Workflow Complete", "FAIL",
                                     f"Status: {wf_status} | Error: {error}")
                        break
        else:
            log.step("Start Workflow", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.step("Start Workflow", "FAIL", str(e))

    # Dashboard summary
    t0 = time.time()
    try:
        r = api("GET", f"{ORCHESTRATOR_URL}/api/v1/orchestrator/dashboard/summary",
                token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            log.step("Dashboard", "PASS",
                     f"Chains: {body.get('total_chains', 0)} | "
                     f"Workflows: {body.get('total_workflows', 0)} | "
                     f"Active: {body.get('active_workflows', 0)} | "
                     f"Success Rate: {body.get('success_rate', '?')}",
                     duration_ms=ms)
    except Exception as e:
        log.step("Dashboard", "FAIL", str(e))

    return result


# ═══════════════════════════════════════════════════════════════
# Demo Orchestration
# ═══════════════════════════════════════════════════════════════

def run_quick_demo():
    """Quick demo: Auth → Shield → Heart (rule extraction + test gen)."""
    print(bold("\n╔═══════════════════════════════════════════════════════════╗"))
    print(bold("║  Nexus QA — Quick Demo (Shield + Heart AI Pipeline)      ║"))
    print(bold("╚═══════════════════════════════════════════════════════════╝"))

    # Health check
    demo_health_check()

    # Authenticate
    token = demo_authenticate()
    if not token:
        print(red("\n  FATAL: Cannot authenticate. Demo aborted."))
        return False

    # Shield: PII Detection & Redaction
    shield_result = demo_shield(token)

    # Heart: Rule Extraction + Test Generation
    heart_result = demo_heart(token, shield_result["redacted_text"])

    return log.summary()


def run_full_demo():
    """Full demo: All engines in sequence."""
    print(bold("\n╔═══════════════════════════════════════════════════════════╗"))
    print(bold("║  Nexus QA — Full Production Demo (All 10 Engines)        ║"))
    print(bold("╚═══════════════════════════════════════════════════════════╝"))
    print(dim("  Scenario: Online Pharmacy Platform — SME Knowledge Capture"))
    print(dim(f"  Started:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))

    # Health check
    demo_health_check()

    # Authenticate
    token = demo_authenticate()
    if not token:
        print(red("\n  FATAL: Cannot authenticate. Demo aborted."))
        return False

    # Phase 2: Shield (PII)
    shield_result = demo_shield(token)

    # Phase 3: Heart (AI — rule extraction, test gen, Q&A)
    heart_result = demo_heart(token, shield_result["redacted_text"])

    # Phase 4: Hands (synthetic data)
    hands_result = demo_hands(token)

    # Phase 5: Backbone (knowledge graph)
    backbone_result = demo_backbone(token, heart_result["rules"])

    # Phase 6: Mouth (report generation)
    mouth_result = demo_mouth(token, heart_result["rules"], heart_result["test_cases"])

    # Phase 7: Platform API (test case CRUD)
    platform_result = demo_platform_api(token)

    return log.summary()


def run_orchestrator_demo():
    """Orchestrator demo: Start a full workflow chain."""
    print(bold("\n╔═══════════════════════════════════════════════════════════╗"))
    print(bold("║  Nexus QA — Orchestrator Pipeline Demo                   ║"))
    print(bold("╚═══════════════════════════════════════════════════════════╝"))
    print(dim("  Runs the Knowledge-Capture chain across multiple engines"))

    # Health check
    demo_health_check()

    # Authenticate
    token = demo_authenticate()
    if not token:
        print(red("\n  FATAL: Cannot authenticate. Demo aborted."))
        return False

    # Orchestrator
    orch_result = demo_orchestrator(token)

    return log.summary()


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Nexus QA — Real E2E Production Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Demo Modes:
              quick        Shield + Heart engines only (~30s)
              full         All engines in sequence (~2-5 min)
              orchestrator Run via orchestrator chain (~3-10 min)

            Environment Variables:
              NEXUS_GATEWAY_URL    Gateway URL (default: http://localhost:8080)
              AUTH_URL             Auth service URL (default: http://localhost:8000)
              PLATFORM_API_URL     Platform API URL (default: http://localhost:8091)
              ORCHESTRATOR_URL     Orchestrator URL (default: http://localhost:8100)
              DEMO_PASSWORD        Demo user password
              NO_COLOR             Disable colored output
        """),
    )
    parser.add_argument(
        "--demo", choices=["quick", "full", "orchestrator"],
        default="full", help="Demo mode (default: full)",
    )
    parser.add_argument(
        "--gateway", default=None,
        help="Override gateway URL",
    )
    args = parser.parse_args()

    if args.gateway:
        global GATEWAY
        GATEWAY = args.gateway

    demos = {
        "quick": run_quick_demo,
        "full": run_full_demo,
        "orchestrator": run_orchestrator_demo,
    }

    success = demos[args.demo]()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
