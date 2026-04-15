"""
Pre-handover validation: tests every API path the frontend UI will call.
Ensures the QI Engineer customer can use the portal without hitting errors.
Routes are matched EXACTLY to the registered FastAPI endpoints.
"""

import httpx
import json
import sys
import time
import os
import tempfile

BASE = "http://localhost:8080"

results = []

def check(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")

# --- 1. Self-Signup ---

section("1. SELF-SIGNUP (New QI Engineer)")

signup_ok = False
try:
    r = httpx.post(f"{BASE}/api/v1/auth/self-signup", json={
        "tenant_name": "Customer QA Org",
        "email": "qi_handover2@nexus.local",
        "password": "Handover2026!",
        "name": "QI Handover Tester",
        "plan": "enterprise",
    }, timeout=15)
    if r.status_code == 200:
        d = r.json()
        check("Self-Signup", True, f"User: {d['email']} | Tenant: {d['tenant_id']}")
        signup_ok = True
    elif r.status_code == 409:
        check("Self-Signup", True, "User already exists (previously created)")
        signup_ok = True
    else:
        check("Self-Signup", False, f"HTTP {r.status_code}: {r.text[:200]}")
except Exception as e:
    check("Self-Signup", False, str(e))

# --- 2. Login ---

section("2. LOGIN")

token = None
tenant_id = None
try:
    for email, pw in [
        ("qi_handover2@nexus.local", "Handover2026!"),
        ("qi_handover@nexus.local", "Handover2026!"),
        ("admin@nexus.local", "change-this-password"),
    ]:
        r = httpx.post(f"{BASE}/api/v1/auth/login", json={"email": email, "password": pw}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            token = d["access_token"]
            user_info = d.get("user", d)
            tenant_id = user_info["tenant_id"]
            check("Login", True, f"User: {email} | Role: {user_info.get('role','?')} | Tenant: {tenant_id[:12]}...")
            break
    if not token:
        check("Login", False, "No credentials worked")
except Exception as e:
    check("Login", False, str(e))

if not token:
    print("\n  FATAL: Cannot continue without auth token.")
    sys.exit(1)

headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# --- 3. Dashboard APIs ---

section("3. DASHBOARD APIs (Page Load)")

for name, port in [("Shield", 8001), ("Ears", 8002), ("Eyes", 8003), ("Heart", 8004),
                    ("Backbone", 8005), ("Nerves", 8006), ("Legs", 8007), ("Hands", 8008),
                    ("Spine", 8009), ("Mouth", 8010), ("Brain", 8011)]:
    try:
        r = httpx.get(f"http://localhost:{port}/health", timeout=5)
        check(f"Engine Health: {name}", r.status_code == 200, f"port {port}")
    except Exception as e:
        check(f"Engine Health: {name}", False, str(e))

# Brain tier status -- actual route is /api/v1/brain/tiers
try:
    r = httpx.get(f"{BASE}/api/v1/brain/tiers", headers=headers, timeout=10)
    if r.status_code == 200:
        d = r.json()
        check("Brain Tier Status", True,
              f"Mode: {d.get('mode', '?')} | Engines: {d.get('total_engines', '?')}")
    else:
        check("Brain Tier Status", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Brain Tier Status", False, str(e))

# --- 4. Shield (PII Detection & Redaction) ---

section("4. SHIELD -- PII Detection & Redaction")

pii_text = "Meeting with John Smith, SSN 111-22-3333, email john@test.com"
try:
    r = httpx.post(f"{BASE}/api/v1/shield/analyze", headers=headers, json={
        "text": pii_text,
        "tenant_id": tenant_id,
        "trace_id": "handover-001",
    }, timeout=10)
    if r.status_code == 200:
        d = r.json()
        entities = d.get("entities", [])
        check("Shield PII Scan", True, f"Found {len(entities)} PII entities")
    else:
        check("Shield PII Scan", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Shield PII Scan", False, str(e))

try:
    r = httpx.post(f"{BASE}/api/v1/shield/redact", headers=headers, json={
        "text": pii_text,
        "tenant_id": tenant_id,
        "trace_id": "handover-002",
    }, timeout=10)
    if r.status_code == 200:
        d = r.json()
        check("Shield Redaction", True, f"Redacted {d.get('entities_redacted', '?')} entities")
    else:
        check("Shield Redaction", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Shield Redaction", False, str(e))

# --- 5. Spine (Document Ingestion) -- uses multipart form ---

section("5. SPINE -- Document Ingestion")

tmp_doc = os.path.join(tempfile.gettempdir(), "handover_test.txt")
with open(tmp_doc, "w") as f:
    f.write("Business Rule BR-001: All prescriptions require pharmacist verification within 24 hours.\n"
            "Business Rule BR-002: Schedule II medications cannot be refilled.\n")

try:
    with open(tmp_doc, "rb") as f:
        upload_headers = {"Authorization": f"Bearer {token}"}
        r = httpx.post(f"{BASE}/api/v1/spine/ingest",
                       headers=upload_headers,
                       files={"file": ("handover_test.txt", f, "text/plain")},
                       data={"tenant_id": tenant_id},
                       timeout=15)
    if r.status_code == 200:
        d = r.json()
        check("Spine Document Ingest", True, f"Doc: {str(d.get('document_id',''))[:12]}...")
    else:
        check("Spine Document Ingest", False, f"HTTP {r.status_code}: {r.text[:150]}")
except Exception as e:
    check("Spine Document Ingest", False, str(e))

try:
    r = httpx.post(f"{BASE}/api/v1/spine/search", headers=headers, json={
        "query": "pharmacist verification",
        "tenant_id": tenant_id,
        "trace_id": "handover-004",
    }, timeout=10)
    check("Spine Search", r.status_code == 200,
          f"Results: {len(r.json().get('results', []))}" if r.status_code == 200 else f"HTTP {r.status_code}")
except Exception as e:
    check("Spine Search", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/spine/stats", headers=headers, timeout=10)
    check("Spine Stats", r.status_code == 200,
          f"Docs: {r.json().get('total_documents', '?')}" if r.status_code == 200 else f"HTTP {r.status_code}")
except Exception as e:
    check("Spine Stats", False, str(e))

# --- 6. Heart (AI Analysis) -- actual routes ---

section("6. HEART -- AI Analysis (Real Ollama LLM)")

try:
    r = httpx.post(f"{BASE}/api/v1/heart/analyze", headers=headers, json={
        "content": "All Schedule II prescriptions must have a valid DEA number. Refills are not permitted.",
        "tenant_id": tenant_id,
        "trace_id": "handover-005",
        "session_id": "handover-session",
    }, timeout=120)
    if r.status_code == 200:
        d = r.json()
        check("Heart Analyze", True,
              f"Rules: {d.get('rules_found', '?')} | Risks: {d.get('risks_found', '?')} ({round(r.elapsed.total_seconds(),1)}s)")
    else:
        check("Heart Analyze", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Heart Analyze", False, str(e))

try:
    r = httpx.post(f"{BASE}/api/v1/heart/ask", headers=headers, json={
        "question": "What are the rules for Schedule II prescriptions?",
        "context": "Schedule II prescriptions cannot be refilled. They require a DEA number.",
        "tenant_id": tenant_id,
        "trace_id": "handover-006",
        "session_id": "handover-session",
    }, timeout=180)
    if r.status_code == 200:
        d = r.json()
        answer = str(d.get("answer", ""))[:80]
        check("Heart AI Q&A", True, f"Confidence: {d.get('confidence', '?')} | Answer: {answer}...")
    else:
        check("Heart AI Q&A", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Heart AI Q&A", False, str(e))

# --- 7. Backbone (Knowledge Graph) -- actual routes ---

section("7. BACKBONE -- Knowledge Graph (Neo4j)")

try:
    r = httpx.post(f"{BASE}/api/v1/backbone/nodes", headers=headers, json={
        "node_type": "business_rule",
        "properties": {"rule_id": "BR-HANDOVER-001", "description": "Handover test rule", "category": "test"},
        "tenant_id": tenant_id,
        "trace_id": "handover-007",
    }, timeout=10)
    if r.status_code == 200:
        d = r.json()
        check("Backbone Store Node", True, f"Node: {str(d.get('node_id', '?'))[:12]}...")
    else:
        check("Backbone Store Node", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Backbone Store Node", False, str(e))

try:
    r = httpx.post(f"{BASE}/api/v1/backbone/search", headers=headers, json={
        "query": "handover test",
        "tenant_id": tenant_id,
        "trace_id": "handover-008",
    }, timeout=10)
    check("Backbone Search", r.status_code == 200,
          f"Results: {r.json().get('total', 0)}" if r.status_code == 200 else f"HTTP {r.status_code}")
except Exception as e:
    check("Backbone Search", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/backbone/stats", headers=headers, timeout=10)
    check("Backbone Stats", r.status_code == 200)
except Exception as e:
    check("Backbone Stats", False, str(e))

# --- 8. Test Case Management ---

section("8. TEST CASE MANAGEMENT (Platform API)")

tc_id = None
try:
    r = httpx.post(f"{BASE}/api/v1/test-cases", headers=headers, json={
        "title": "Handover Validation Test",
        "test_id": "HO-001",
        "description": "Verify Schedule II prescription cannot be refilled",
        "steps": [
            {"step_number": 1, "action": "Open prescription", "expected": "Form loads"},
            {"step_number": 2, "action": "Click refill", "expected": "Error: refill not allowed for Schedule II"},
        ],
        "priority": "high",
        "category": "regulatory",
        "tenant_id": tenant_id,
        "trace_id": "handover-009",
    }, timeout=10)
    if r.status_code in (200, 201):
        d = r.json()
        tc_id = d.get("id") or d.get("test_id") or d.get("test_case_id")
        check("Create Test Case", True, f"ID: {tc_id}")
    else:
        check("Create Test Case", False, f"HTTP {r.status_code}: {r.text[:150]}")
except Exception as e:
    check("Create Test Case", False, str(e))

try:
    r = httpx.post(f"{BASE}/api/v1/test-cases/export", headers=headers, json={"format": "json", "tenant_id": tenant_id}, timeout=10)
    if r.status_code == 200:
        d = r.json()
        count = len(d) if isinstance(d, list) else d.get("count", d.get("total", "?"))
        check("Export Test Cases", True, f"Records: {count}")
    else:
        check("Export Test Cases", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Export Test Cases", False, str(e))

# --- 9. Report Generation (Mouth) ---

section("9. REPORT GENERATION (Mouth)")

try:
    r = httpx.post(f"{BASE}/api/v1/mouth/generate", headers=headers, json={
        "title": "Handover QA Report",
        "session_id": "handover-session",
        "tenant_id": tenant_id,
        "trace_id": "handover-010",
        "report_type": "executive_summary",
        "format": "html",
    }, timeout=10)
    if r.status_code == 200:
        d = r.json()
        check("Generate Report", True, f"Report: {d.get('report_id', '?')}")
    else:
        check("Generate Report", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Generate Report", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/mouth/stats", headers=headers, timeout=10)
    if r.status_code == 200:
        d = r.json()
        check("Report Stats", True, f"Total: {d.get('total_reports', '?')} | Formats: {d.get('supported_formats', '?')}")
    else:
        check("Report Stats", False, f"HTTP {r.status_code}")
except Exception as e:
    check("Report Stats", False, str(e))

# --- 10. Hands (Test Data) ---

section("10. HANDS -- Synthetic Test Data")

try:
    r = httpx.post(f"{BASE}/api/v1/hands/generate-profiles", headers=headers, json={
        "count": 3,
        "tenant_id": tenant_id,
        "trace_id": "handover-011",
    }, timeout=10)
    if r.status_code == 200:
        d = r.json()
        profiles = d.get("profiles", d.get("data", []))
        check("Generate Test Data", True, f"{len(profiles)} profiles")
    else:
        check("Generate Test Data", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Generate Test Data", False, str(e))

# --- 11. Connectors (Nerves) ---

section("11. NERVES -- Connectors")

try:
    r = httpx.get(f"{BASE}/api/v1/nerves/connectors", headers=headers, timeout=10)
    if r.status_code == 200:
        d = r.json()
        conns = d if isinstance(d, list) else d.get("connectors", [])
        check("List Connectors", True, f"{len(conns)} available")
    else:
        check("List Connectors", False, f"HTTP {r.status_code}")
except Exception as e:
    check("List Connectors", False, str(e))

# --- 12. Brain Coordinator ---

section("12. BRAIN -- Intelligent Coordinator")

try:
    r = httpx.post(f"{BASE}/api/v1/brain/quality-gate", headers=headers, json={
        "session_id": "handover-session",
        "tenant_id": tenant_id,
        "trace_id": "handover-012",
        "rules_count": 2,
        "test_cases_count": 1,
        "coverage_percent": 50.0,
    }, timeout=15)
    if r.status_code == 200:
        d = r.json()
        check("Quality Gate", True, f"Score: {d.get('score', '?')} | Level: {d.get('quality_level', '?')}")
    else:
        check("Quality Gate", False, f"HTTP {r.status_code}: {r.text[:100]}")
except Exception as e:
    check("Quality Gate", False, str(e))

# --- 13. Platform API Module Routes ---

section("13. PLATFORM API -- Module Routes")

try:
    r = httpx.get(f"{BASE}/api/v1/sessions", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("Sessions List", r.status_code == 200,
          f"Count: {len(r.json()) if isinstance(r.json(), list) else r.json().get('total', '?')}" if r.status_code == 200 else f"HTTP {r.status_code}")
except Exception as e:
    check("Sessions List", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/sme/profiles", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("SME Profiles", r.status_code == 200)
except Exception as e:
    check("SME Profiles", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/contradictions", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("Contradictions List", r.status_code == 200)
except Exception as e:
    check("Contradictions List", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/guardrails/pipeline", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("Guardrails Pipeline", r.status_code == 200)
except Exception as e:
    check("Guardrails Pipeline", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/insights/kpis", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("Insights KPIs", r.status_code == 200)
except Exception as e:
    check("Insights KPIs", False, str(e))

try:
    r = httpx.get(f"{BASE}/api/v1/compliance/jurisdictions", headers=headers, params={"tenant_id": tenant_id}, timeout=10)
    check("Compliance Jurisdictions", r.status_code == 200)
except Exception as e:
    check("Compliance Jurisdictions", False, str(e))

# --- SUMMARY ---

print(f"\n{'='*60}")
print(f"  PRE-HANDOVER VALIDATION SUMMARY")
print(f"{'='*60}\n")

passed = sum(1 for _, p, _ in results if p)
failed = sum(1 for _, p, _ in results if not p)
total = len(results)

print(f"  PASSED:  {passed}")
print(f"  FAILED:  {failed}")
print(f"  TOTAL:   {total}")
print()

if failed > 0:
    print("  FAILURES:")
    for name, p, detail in results:
        if not p:
            print(f"    - {name}: {detail}")
    print()

if failed == 0:
    print("  *** READY FOR CUSTOMER HANDOVER ***\n")
else:
    print(f"  *** {failed} ISSUE(S) NEED FIXING BEFORE HANDOVER ***\n")
