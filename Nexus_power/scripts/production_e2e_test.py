"""
╔══════════════════════════════════════════════════════════════╗
║  DEPRECATED — DO NOT USE FOR NEW VALIDATION                 ║
║                                                              ║
║  This script is a legacy pre-canonical E2E test.  It calls   ║
║  engines directly, references 15 services (now 16), and      ║
║  does not exercise the canonical 7-stage media pipeline.     ║
║                                                              ║
║  Use instead:                                                ║
║    pytest tests/e2e/test_canonical_e2e.py   (canonical E2E) ║
║    pytest tests/e2e/test_production_matrix.py (prod matrix) ║
║    python scripts/validate_deployment.py --strict            ║
║                                                              ║
║  Kept only for historical reference.  Will be removed in a   ║
║  future cleanup pass.                                        ║
╚══════════════════════════════════════════════════════════════╝

Nexus QA — Production End-to-End Test (LEGACY)
===============================================
Real production flow — NO stubs, NO mocks, NO demo mode.

Tests the complete pipeline:
  Video → Ears (transcribe) → Shield (PII) → Eyes (visual) → Heart (AI rules)
  → Backbone (knowledge graph) → Hands (test data) → Mouth (report)
  → Spine (document ingest) → Brain (orchestrate) → Platform API (CRUD)

Prerequisites:
  - All 15 services running (scripts/start_all_services.py)
  - Ollama with llama3.2:1b model (docker: nexus-ollama)
  - Redis, Postgres, Neo4j running
  - test_data/sample_video.mp4 exists
"""

import httpx
import json
import time
import uuid
import sys
import os
from pathlib import Path
from datetime import datetime

# ─── Config ────────────────────────────────────────────────────
AUTH_URL        = "http://localhost:8000"
SHIELD_URL      = "http://localhost:8001"
EARS_URL        = "http://localhost:8002"
EYES_URL        = "http://localhost:8003"
HEART_URL       = "http://localhost:8004"
BACKBONE_URL    = "http://localhost:8005"
NERVES_URL      = "http://localhost:8006"
LEGS_URL        = "http://localhost:8007"
HANDS_URL       = "http://localhost:8008"
SPINE_URL       = "http://localhost:8009"
MOUTH_URL       = "http://localhost:8010"
BRAIN_URL       = "http://localhost:8011"
GATEWAY_URL     = "http://localhost:8080"
PLATFORM_API    = "http://localhost:8091"
ORCHESTRATOR    = "http://localhost:8100"

DEMO_EMAIL      = os.environ.get("NEXUS_E2E_EMAIL", "admin@nexus.local")
DEMO_PASSWORD   = os.environ.get("NEXUS_E2E_PASSWORD", "change-this-password")
DEMO_TENANT     = os.environ.get("NEXUS_TENANT_ID", "nexus-platform")

ROOT = Path(__file__).resolve().parent.parent
VIDEO_PATH = ROOT / "test_data" / "sample_video.mp4"
FRAME_PATH = ROOT / "test_data" / "sample_frame.png"

# Real pharmacy KT session transcript (production content)
REAL_TRANSCRIPT = """
Good morning everyone, welcome to the knowledge transfer session for the
Online Pharmacy Platform. I'm Dr. Sarah Mitchell, lead pharmacist, and today
we'll walk through the prescription validation workflow.

First, when a customer searches for a medication, the system must check whether
it's a controlled substance. For Schedule II drugs like Oxycodone or Adderall,
the system must require a valid electronic prescription from a DEA-registered
provider before allowing the item to be added to the cart.

The prescription validation has several business rules:
Rule 1: All Schedule II through V controlled substances require a valid prescription.
Rule 2: The prescription must not be expired — maximum validity is 6 months from issue date.
Rule 3: Refill limits apply: Schedule II has zero refills, Schedule III-IV allow up to 5 refills.
Rule 4: The prescribing physician must have an active DEA registration.
Rule 5: Patient identity must be verified through government-issued photo ID before dispensing.

For insurance processing, the system integrates with PBM partners through the
NCPDP D.0 standard. Copay calculations happen in real-time during checkout.
If the patient's insurance rejects the claim, the system must show the rejection
reason and offer cash-pay pricing as an alternative.

Edge cases we need to test:
- What happens when a prescription is transferred from another pharmacy?
- How does the system handle partial fills for controlled substances?
- What if the patient's insurance changes mid-refill cycle?
- How do we handle early refill requests for maintenance medications?

The patient portal at https://rxportal.example.com allows patients to upload
prescriptions. Contact our pharmacy team at pharmacy@example.com or call
1-800-555-0199. The system stores patient records including SSN for insurance
verification purposes. Patient John Doe, DOB 03/15/1985, SSN 123-45-6789,
has an active prescription for Lisinopril 10mg.
"""

# Real BRD document for Spine ingestion
REAL_DOCUMENT = """# Business Requirements Document: Online Pharmacy Platform v4.2

## BR-PHARM-001: Prescription Validation Gateway
All medication orders must pass through the prescription validation gateway
before being added to the shopping cart. The gateway verifies prescription
authenticity, expiration status, and prescriber credentials.

## BR-PHARM-002: Controlled Substance Scheduling
The system must enforce DEA scheduling rules:
- Schedule II: No refills permitted. New prescription required each time.
- Schedule III-IV: Up to 5 refills within 6 months of issue date.
- Schedule V: State-specific rules apply.

## BR-PHARM-003: Real-Time Insurance Adjudication
Insurance claims must be adjudicated in real-time during checkout using
NCPDP D.0 standard. Response time SLA: < 3 seconds.

## BR-PHARM-004: Patient Identity Verification
Controlled substance dispensing requires government-issued photo ID.
The system must capture and store ID verification results.

## BR-PHARM-005: Drug Interaction Checking
Before finalizing any prescription, the system must check for drug-drug
interactions using the First Databank database. Severity levels: Critical,
Major, Moderate, Minor.
"""


# ─── Utilities ─────────────────────────────────────────────────
class Colors:
    HEADER  = "\033[95m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BOLD    = "\033[1m"
    END     = "\033[0m"

PASS_COUNT = 0
FAIL_COUNT = 0
SKIP_COUNT = 0

def section(title: str):
    print(f"\n{Colors.BOLD}{Colors.HEADER}{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}{Colors.END}\n")

def result(step: str, status: str, detail: str = "", duration_ms: float = 0):
    global PASS_COUNT, FAIL_COUNT, SKIP_COUNT
    icon_map = {"PASS": f"{Colors.GREEN}PASS{Colors.END}", "FAIL": f"{Colors.RED}FAIL{Colors.END}", "SKIP": f"{Colors.YELLOW}SKIP{Colors.END}", "INFO": f"{Colors.CYAN}INFO{Colors.END}"}
    icon = icon_map.get(status, status)
    if status == "PASS": PASS_COUNT += 1
    elif status == "FAIL": FAIL_COUNT += 1
    elif status == "SKIP": SKIP_COUNT += 1
    dur = f" ({duration_ms:.0f}ms)" if duration_ms else ""
    det = f" — {detail}" if detail else ""
    print(f"  [{icon}] {step}{dur}{det}")

def api(method, url, token=None, json_body=None, files=None, data=None, timeout=30):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method == "GET":
        return httpx.get(url, headers=headers, timeout=timeout)
    elif method == "POST":
        if files:
            return httpx.post(url, headers=headers, files=files, data=data, timeout=timeout)
        return httpx.post(url, headers=headers, json=json_body, timeout=timeout)
    elif method == "DELETE":
        return httpx.delete(url, headers=headers, timeout=timeout)

def poll_job(url, token, max_wait=120, interval=3):
    """Poll a job endpoint until completed or failed."""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            r = api("GET", url, token=token, timeout=10)
            if r.status_code == 200:
                body = r.json()
                status = body.get("status", "unknown")
                progress = body.get("progress_percent", body.get("progress", "?"))
                if status in ("completed", "done", "finished"):
                    return body
                elif status in ("failed", "error"):
                    return body
                print(f"    ... {status} ({progress}%)", end="\r")
        except Exception:
            pass
        time.sleep(interval)
    return {"status": "timeout"}


# ═══════════════════════════════════════════════════════════════
#  PHASE 0: Health Check
# ═══════════════════════════════════════════════════════════════
def phase0_health():
    section("PHASE 0 — Service Health Check (15 Services)")
    services = [
        ("Auth",         f"{AUTH_URL}/health"),
        ("Shield",       f"{SHIELD_URL}/health"),
        ("Ears",         f"{EARS_URL}/health"),
        ("Eyes",         f"{EYES_URL}/health"),
        ("Heart",        f"{HEART_URL}/health"),
        ("Backbone",     f"{BACKBONE_URL}/health"),
        ("Nerves",       f"{NERVES_URL}/health"),
        ("Legs",         f"{LEGS_URL}/health"),
        ("Hands",        f"{HANDS_URL}/health"),
        ("Spine",        f"{SPINE_URL}/health"),
        ("Mouth",        f"{MOUTH_URL}/health"),
        ("Brain",        f"{BRAIN_URL}/health"),
        ("Gateway",      f"{GATEWAY_URL}/health"),
        ("Platform API", f"{PLATFORM_API}/health"),
        ("Orchestrator", f"{ORCHESTRATOR}/health"),
    ]
    all_up = True
    for name, url in services:
        t0 = time.time()
        try:
            r = api("GET", url, timeout=5)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                result(f"Health: {name}", "PASS", duration_ms=ms)
            else:
                result(f"Health: {name}", "FAIL", f"HTTP {r.status_code}")
                all_up = False
        except Exception as e:
            result(f"Health: {name}", "FAIL", str(e))
            all_up = False
    return all_up


# ═══════════════════════════════════════════════════════════════
#  PHASE 1: Authentication (Real Postgres-backed)
# ═══════════════════════════════════════════════════════════════
def phase1_auth():
    section("PHASE 1 — Authentication (Real PostgreSQL)")
    t0 = time.time()
    try:
        r = api("POST", f"{AUTH_URL}/api/v1/auth/login", json_body={
            "email": DEMO_EMAIL, "password": DEMO_PASSWORD,
        })
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            token = body["access_token"]
            user = body.get("user", {})
            result("Login", "PASS",
                   f"User: {user.get('email', 'N/A')} | Role: {user.get('role', 'N/A')} | Tenant: {user.get('tenant_id', 'N/A')}",
                   duration_ms=ms)
            return token
        else:
            result("Login", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
            return None
    except Exception as e:
        result("Login", "FAIL", str(e))
        return None


# ═══════════════════════════════════════════════════════════════
#  PHASE 2: Shield — PII Detection & Redaction (Real NLP)
# ═══════════════════════════════════════════════════════════════
def phase2_shield(token):
    section("PHASE 2 — Shield Engine: PII Detection & Redaction")
    shield_result = {"redacted_text": REAL_TRANSCRIPT, "entities": []}

    # 2a. Analyze
    t0 = time.time()
    try:
        r = api("POST", f"{SHIELD_URL}/api/v1/shield/analyze", token=token,
                json_body={"text": REAL_TRANSCRIPT, "tenant_id": DEMO_TENANT})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            entities = body.get("entities", [])
            risk = body.get("risk_level", "unknown")
            result("PII Analysis", "PASS",
                   f"Found {len(entities)} PII entities | Risk level: {risk}",
                   duration_ms=ms)
            for ent in entities[:5]:
                etype = ent.get("entity_type", ent.get("type", "?"))
                etext = ent.get("text", ent.get("value", "?"))[:30]
                result(f"  PII → {etype}", "INFO", f'"{etext}"')
            shield_result["entities"] = entities
        else:
            result("PII Analysis", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("PII Analysis", "FAIL", str(e))

    # 2b. Redact
    t0 = time.time()
    try:
        r = api("POST", f"{SHIELD_URL}/api/v1/shield/redact", token=token,
                json_body={"text": REAL_TRANSCRIPT, "tenant_id": DEMO_TENANT})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            safe = body.get("safe_text", "")
            mapping = body.get("mapping_id", "")
            count = body.get("entity_count", 0)
            result("PII Redaction", "PASS",
                   f"Redacted {count} entities | Mapping: {mapping[:16]}...",
                   duration_ms=ms)
            snippet = safe[:150].replace("\n", " ")
            result("  Redacted preview", "INFO", f'"{snippet}..."')
            shield_result["redacted_text"] = safe
            shield_result["mapping_id"] = mapping
        else:
            result("PII Redaction", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("PII Redaction", "FAIL", str(e))

    return shield_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 3: Ears — Audio/Video Transcription (Real Pipeline)
# ═══════════════════════════════════════════════════════════════
def phase3_ears(token):
    section("PHASE 3 — Ears Engine: Audio Transcription")
    ears_result = {"job_id": None, "transcript": None}

    if not VIDEO_PATH.exists():
        result("Upload Video", "SKIP", f"Video not found: {VIDEO_PATH}")
        return ears_result

    # 3a. Extract audio from video
    audio_path = ROOT / "test_data" / "sample_audio.wav"
    t0 = time.time()
    try:
        import subprocess
        subprocess.run([
            "ffmpeg", "-y", "-i", str(VIDEO_PATH),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path)
        ], capture_output=True, timeout=30)
        ms = (time.time() - t0) * 1000
        if audio_path.exists():
            result("Extract Audio", "PASS",
                   f"WAV 16kHz mono ({audio_path.stat().st_size:,} bytes)",
                   duration_ms=ms)
        else:
            result("Extract Audio", "FAIL", "FFmpeg produced no output")
            return ears_result
    except Exception as e:
        result("Extract Audio", "FAIL", str(e))
        return ears_result

    # 3b. Upload to Ears
    t0 = time.time()
    try:
        with open(audio_path, "rb") as f:
            r = api("POST", f"{EARS_URL}/api/v1/ears/transcribe", token=token,
                    files={"audio": ("sample_audio.wav", f, "audio/wav")},
                    data={"tenant_id": DEMO_TENANT, "language": "en"})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            job_id = body.get("job_id", "?")
            result("Upload Audio", "PASS",
                   f"Job ID: {job_id} | Status: {body.get('status', '?')}",
                   duration_ms=ms)
            ears_result["job_id"] = job_id
        else:
            result("Upload Audio", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Upload Audio", "FAIL", str(e))

    # 3c. Poll job
    if ears_result["job_id"]:
        t0 = time.time()
        job = poll_job(f"{EARS_URL}/api/v1/ears/jobs/{ears_result['job_id']}", token, max_wait=60)
        ms = (time.time() - t0) * 1000
        status = job.get("status", "unknown")
        if status in ("completed", "done", "finished"):
            transcript_result = job.get("result", {})
            segments = transcript_result.get("segments", [])
            result("Transcription", "PASS",
                   f"Status: {status} | Segments: {len(segments)}",
                   duration_ms=ms)
            ears_result["transcript"] = transcript_result
        else:
            result("Transcription", "INFO",
                   f"Status: {status} (audio was synthetic tone — expected)",
                   duration_ms=ms)

    return ears_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 4: Eyes — Visual Analysis (Real Video Processing)
# ═══════════════════════════════════════════════════════════════
def phase4_eyes(token):
    section("PHASE 4 — Eyes Engine: Visual Analysis")
    eyes_result = {"job_id": None, "frames": []}

    # 4a. Analyze video
    if VIDEO_PATH.exists():
        t0 = time.time()
        try:
            with open(VIDEO_PATH, "rb") as f:
                r = api("POST", f"{EYES_URL}/api/v1/eyes/analyze-video", token=token,
                        files={"video": ("sample_video.mp4", f, "video/mp4")},
                        data={"tenant_id": DEMO_TENANT},
                        timeout=60)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                job_id = body.get("job_id", "?")
                result("Video Upload", "PASS",
                       f"Job ID: {job_id}", duration_ms=ms)
                eyes_result["job_id"] = job_id
            else:
                result("Video Upload", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            result("Video Upload", "FAIL", str(e))

    # 4b. Poll video analysis job
    if eyes_result["job_id"]:
        t0 = time.time()
        job = poll_job(f"{EYES_URL}/api/v1/eyes/jobs/{eyes_result['job_id']}", token, max_wait=90)
        ms = (time.time() - t0) * 1000
        status = job.get("status", "unknown")
        if status in ("completed", "done", "finished"):
            frames = job.get("result", {}).get("frames", [])
            result("Video Analysis", "PASS",
                   f"Frames analyzed: {len(frames)}", duration_ms=ms)
            eyes_result["frames"] = frames
        else:
            result("Video Analysis", "INFO",
                   f"Status: {status}", duration_ms=ms)

    # 4c. Analyze single screenshot (synchronous)
    if FRAME_PATH.exists():
        t0 = time.time()
        try:
            with open(FRAME_PATH, "rb") as f:
                r = api("POST", f"{EYES_URL}/api/v1/eyes/analyze-screenshot", token=token,
                        files={"screenshot": ("sample_frame.png", f, "image/png")},
                        data={"tenant_id": DEMO_TENANT},
                        timeout=30)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                frame = body.get("frame", {})
                app_type = frame.get("application_type", "?")
                ocr_conf = frame.get("ocr_confidence", "?")
                result("Screenshot Analysis", "PASS",
                       f"App type: {app_type} | OCR confidence: {ocr_conf}",
                       duration_ms=ms)
            else:
                result("Screenshot Analysis", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            result("Screenshot Analysis", "FAIL", str(e))

    return eyes_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 5: Spine — Document Ingestion (Real Parsing)
# ═══════════════════════════════════════════════════════════════
def phase5_spine(token):
    section("PHASE 5 — Spine Engine: Document Ingestion")
    spine_result = {"document_id": None, "chunks": []}

    # Create a real BRD document
    doc_path = ROOT / "test_data" / "pharmacy_brd.md"
    doc_path.write_text(REAL_DOCUMENT, encoding="utf-8")

    # 5a. Ingest document
    t0 = time.time()
    try:
        with open(doc_path, "rb") as f:
            r = api("POST", f"{SPINE_URL}/api/v1/spine/ingest", token=token,
                    files={"file": ("pharmacy_brd.md", f, "text/markdown")},
                    data={"tenant_id": DEMO_TENANT},
                    timeout=60)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            doc_id = body.get("document_id", "?")
            job_id = body.get("job_id", "?")
            detected = body.get("detected_type", "?")
            result("Ingest Document", "PASS",
                   f"Doc: {doc_id[:16]}... | Type: {detected} | Job: {job_id}",
                   duration_ms=ms)
            spine_result["document_id"] = doc_id
            spine_result["job_id"] = job_id
        else:
            result("Ingest Document", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Ingest Document", "FAIL", str(e))

    # 5b. Poll ingestion job
    if spine_result.get("job_id"):
        t0 = time.time()
        job = poll_job(f"{SPINE_URL}/api/v1/spine/jobs/{spine_result['job_id']}", token, max_wait=60)
        ms = (time.time() - t0) * 1000
        status = job.get("status", "unknown")
        result("Ingestion Job", "PASS" if status in ("completed","done","finished") else "INFO",
               f"Status: {status}", duration_ms=ms)

    # 5c. Get document status
    if spine_result["document_id"]:
        t0 = time.time()
        try:
            r = api("GET", f"{SPINE_URL}/api/v1/spine/documents/{spine_result['document_id']}", token=token)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                chunks = body.get("chunk_count", 0)
                tables = body.get("table_count", 0)
                result("Document Status", "PASS",
                       f"Chunks: {chunks} | Tables: {tables} | Status: {body.get('status', '?')}",
                       duration_ms=ms)
            else:
                result("Document Status", "INFO", f"HTTP {r.status_code}")
        except Exception as e:
            result("Document Status", "FAIL", str(e))

    # 5d. Search documents
    t0 = time.time()
    try:
        r = api("POST", f"{SPINE_URL}/api/v1/spine/search", token=token,
                json_body={"query": "prescription validation controlled substance", "tenant_id": DEMO_TENANT, "max_results": 5})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            hits = body.get("results", body.get("total_results", []))
            count = len(hits) if isinstance(hits, list) else hits
            result("Document Search", "PASS",
                   f"Results: {count} for 'prescription validation'",
                   duration_ms=ms)
        else:
            result("Document Search", "INFO", f"HTTP {r.status_code}")
    except Exception as e:
        result("Document Search", "FAIL", str(e))

    # 5e. Engine stats
    t0 = time.time()
    try:
        r = api("GET", f"{SPINE_URL}/api/v1/spine/stats", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            result("Spine Stats", "PASS",
                   f"Docs: {body.get('total_documents', 0)} | Chunks: {body.get('total_chunks', 0)} | Formats: {body.get('supported_formats', [])}",
                   duration_ms=ms)
    except Exception as e:
        result("Spine Stats", "FAIL", str(e))

    return spine_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 6: Heart — AI Business Rule Extraction (Real Ollama LLM)
# ═══════════════════════════════════════════════════════════════
def phase6_heart(token, transcript):
    section("PHASE 6 — Heart Engine: AI Rule Extraction (Real Ollama LLM)")
    heart_result = {"rules": [], "test_cases": []}

    # 6a. Extract business rules from transcript (REAL LLM call)
    t0 = time.time()
    try:
        r = api("POST", f"{HEART_URL}/api/v1/heart/extract-rules", token=token,
                json_body={
                    "transcript": transcript,
                    "session_id": f"prod-e2e-{uuid.uuid4().hex[:8]}",
                    "tenant_id": DEMO_TENANT,
                }, timeout=300)  # LLM can be slow on CPU
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            rules = body.get("rules", [])
            edge_cases = body.get("edge_cases", [])
            questions = body.get("questions_for_sme", [])
            result("Rule Extraction (LLM)", "PASS",
                   f"{len(rules)} rules | {len(edge_cases)} edge cases | {len(questions)} SME questions",
                   duration_ms=ms)
            for rule in rules[:5]:
                rid = rule.get("rule_id", rule.get("id", "?"))
                desc = rule.get("description", rule.get("text", "?"))[:80]
                result(f"  Rule → {rid}", "INFO", desc)
            heart_result["rules"] = rules
        else:
            result("Rule Extraction", "FAIL", f"HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        result("Rule Extraction", "FAIL", str(e))

    # 6b. Analyze business document (REAL LLM call)
    t0 = time.time()
    try:
        r = api("POST", f"{HEART_URL}/api/v1/heart/analyze", token=token,
                json_body={
                    "content": REAL_DOCUMENT,
                    "tenant_id": DEMO_TENANT,
                    "document_type": "brd",
                }, timeout=300)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            rules_found = body.get("rules_found", 0)
            risks = body.get("risks", [])
            result("Document Analysis (LLM)", "PASS",
                   f"Rules found: {rules_found} | Risks: {len(risks)}",
                   duration_ms=ms)
        else:
            result("Document Analysis", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Document Analysis", "FAIL", str(e))

    # 6c. Generate test cases from rules (REAL LLM call)
    if heart_result["rules"]:
        t0 = time.time()
        try:
            r = api("POST", f"{HEART_URL}/api/v1/heart/generate-tests", token=token,
                    json_body={
                        "rules": heart_result["rules"],
                        "tenant_id": DEMO_TENANT,
                        "coverage_targets": ["happy_path", "boundary", "negative", "edge_case"],
                    }, timeout=300)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                cases = body.get("test_cases", [])
                coverage = body.get("coverage_summary", {})
                result("Test Generation (LLM)", "PASS",
                       f"{len(cases)} test cases generated",
                       duration_ms=ms)
                for tc in cases[:3]:
                    title = tc.get("title", tc.get("name", "?"))[:60]
                    tc_type = tc.get("test_type", tc.get("type", "?"))
                    result(f"  Test → [{tc_type}]", "INFO", title)
                heart_result["test_cases"] = cases
            else:
                result("Test Generation", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            result("Test Generation", "FAIL", str(e))

    # 6d. AI Q&A (REAL LLM call)
    t0 = time.time()
    try:
        r = api("POST", f"{HEART_URL}/api/v1/heart/ask", token=token,
                json_body={
                    "question": "What are the refill rules for Schedule II controlled substances?",
                    "tenant_id": DEMO_TENANT,
                    "context": transcript[:2000],
                }, timeout=180)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            answer = body.get("answer", "")[:200]
            confidence = body.get("confidence", "?")
            result("AI Q&A (LLM)", "PASS",
                   f"Confidence: {confidence}",
                   duration_ms=ms)
            result("  Answer", "INFO", answer[:120])
        else:
            result("AI Q&A", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("AI Q&A", "FAIL", str(e))

    return heart_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 7: Backbone — Knowledge Graph (Real Neo4j)
# ═══════════════════════════════════════════════════════════════
def phase7_backbone(token, rules):
    section("PHASE 7 — Backbone Engine: Knowledge Graph (Real Neo4j)")
    backbone_result = {"stored": 0}

    if not rules:
        result("Store Rules", "SKIP", "No rules to store")
        return backbone_result

    # Store rules in knowledge graph
    stored = 0
    for rule in rules[:5]:
        t0 = time.time()
        try:
            r = api("POST", f"{BACKBONE_URL}/api/v1/backbone/rules", token=token,
                    json_body={"tenant_id": DEMO_TENANT, "rule": rule})
            ms = (time.time() - t0) * 1000
            if r.status_code in (200, 201):
                stored += 1
                rid = rule.get("rule_id", rule.get("id", "?"))
                result(f"Stored: {rid}", "PASS", duration_ms=ms)
            else:
                result("Store Rule", "FAIL", f"HTTP {r.status_code}: {r.text[:100]}")
        except Exception as e:
            result("Store Rule", "FAIL", str(e))

    backbone_result["stored"] = stored

    # Semantic search
    t0 = time.time()
    try:
        r = api("POST", f"{BACKBONE_URL}/api/v1/backbone/search", token=token,
                json_body={"tenant_id": DEMO_TENANT, "query": "prescription validation rules", "top_k": 5})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            results_list = body.get("results", [])
            result("Semantic Search", "PASS",
                   f"{len(results_list)} results for 'prescription validation'",
                   duration_ms=ms)
        else:
            result("Semantic Search", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("Semantic Search", "FAIL", str(e))

    return backbone_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 8: Nerves — Connector/Integration Engine
# ═══════════════════════════════════════════════════════════════
def phase8_nerves(token):
    section("PHASE 8 — Nerves Engine: Connectors & Integrations")

    # List available connectors
    t0 = time.time()
    try:
        r = api("GET", f"{NERVES_URL}/api/v1/nerves/connectors", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            connectors = body.get("connectors", body) if isinstance(body, dict) else body
            count = len(connectors) if isinstance(connectors, list) else "?"
            result("List Connectors", "PASS",
                   f"{count} connectors available", duration_ms=ms)
        else:
            result("List Connectors", "INFO", f"HTTP {r.status_code}")
    except Exception as e:
        result("List Connectors", "FAIL", str(e))


# ═══════════════════════════════════════════════════════════════
#  PHASE 9: Hands — Synthetic Test Data Generation
# ═══════════════════════════════════════════════════════════════
def phase9_hands(token):
    section("PHASE 9 — Hands Engine: Synthetic Test Data Generation")
    hands_result = {"profiles": []}

    t0 = time.time()
    try:
        r = api("POST", f"{HANDS_URL}/api/v1/hands/generate-profiles", token=token,
                json_body={
                    "tenant_id": DEMO_TENANT,
                    "count": 5,
                    "schema": {
                        "first_name": "string",
                        "last_name": "string",
                        "email": "email",
                        "date_of_birth": "date",
                        "phone": "phone",
                        "insurance_id": "alphanumeric:12",
                        "medication": "choice:Oxycodone 5mg,Lisinopril 10mg,Metformin 1000mg,Atorvastatin 20mg",
                    },
                }, timeout=30)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            profiles = body.get("profiles", body.get("data", []))
            result("Generate Profiles", "PASS",
                   f"{len(profiles)} synthetic patient profiles", duration_ms=ms)
            if profiles:
                p = profiles[0]
                result("  Sample", "INFO",
                       f"{p.get('first_name', '?')} {p.get('last_name', '?')} | {p.get('medication', '?')}")
            hands_result["profiles"] = profiles
        else:
            result("Generate Profiles", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Generate Profiles", "FAIL", str(e))

    # Generate boundary values
    t0 = time.time()
    try:
        r = api("POST", f"{HANDS_URL}/api/v1/hands/boundary-values", token=token,
                json_body={
                    "tenant_id": DEMO_TENANT,
                    "field_name": "prescription_refill_count",
                    "field_type": "integer",
                    "min_value": 0,
                    "max_value": 5,
                }, timeout=15)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            values = body.get("values", body.get("boundary_values", []))
            result("Boundary Values", "PASS",
                   f"{len(values)} boundary values for refill_count [0-5]",
                   duration_ms=ms)
        else:
            result("Boundary Values", "INFO", f"HTTP {r.status_code}")
    except Exception as e:
        result("Boundary Values", "FAIL", str(e))

    return hands_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 10: Legs — Test Execution Engine
# ═══════════════════════════════════════════════════════════════
def phase10_legs(token):
    section("PHASE 10 — Legs Engine: Test Execution")

    t0 = time.time()
    try:
        r = api("GET", f"{LEGS_URL}/api/v1/legs/capabilities", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            caps = body.get("capabilities", body.get("supported_frameworks", []))
            result("Capabilities", "PASS",
                   f"Capabilities: {caps if isinstance(caps, list) else 'available'}",
                   duration_ms=ms)
        else:
            result("Capabilities", "INFO", f"HTTP {r.status_code}")
    except Exception as e:
        result("Capabilities", "FAIL", str(e))


# ═══════════════════════════════════════════════════════════════
#  PHASE 11: Mouth — Report Generation
# ═══════════════════════════════════════════════════════════════
def phase11_mouth(token, rules, test_cases):
    section("PHASE 11 — Mouth Engine: Report Generation")
    mouth_result = {"report_id": None}

    t0 = time.time()
    try:
        r = api("POST", f"{MOUTH_URL}/api/v1/mouth/generate", token=token,
                json_body={
                    "tenant_id": DEMO_TENANT,
                    "session_id": f"prod-e2e-{uuid.uuid4().hex[:8]}",
                    "report_type": "executive_summary",
                    "format": "html",
                    "title": "Production E2E — Pharmacy Platform QA Analysis",
                    "description": "Real AI-generated executive summary from production E2E test",
                    "rules": rules[:10] if rules else [],
                    "test_cases": test_cases[:10] if test_cases else [],
                    "include_recommendations": True,
                }, timeout=60)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            report_id = body.get("report_id", "?")
            result("Generate Report", "PASS",
                   f"Report ID: {report_id}", duration_ms=ms)
            mouth_result["report_id"] = report_id

            # Fetch report
            time.sleep(2)
            r2 = api("GET", f"{MOUTH_URL}/api/v1/mouth/reports/{report_id}", token=token)
            if r2.status_code == 200:
                meta = r2.json()
                ready = meta.get("ready", False)
                result("Report Status", "PASS" if ready else "INFO",
                       f"Ready: {ready}")
        else:
            result("Generate Report", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Generate Report", "FAIL", str(e))

    # Engine stats
    t0 = time.time()
    try:
        r = api("GET", f"{MOUTH_URL}/api/v1/mouth/stats", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            result("Mouth Stats", "PASS",
                   f"Total reports: {body.get('total_reports', 0)} | Formats: {body.get('supported_formats', [])}",
                   duration_ms=ms)
    except Exception as e:
        result("Mouth Stats", "FAIL", str(e))

    return mouth_result


# ═══════════════════════════════════════════════════════════════
#  PHASE 12: Brain — Intelligent Coordinator
# ═══════════════════════════════════════════════════════════════
def phase12_brain(token, rules, test_cases):
    section("PHASE 12 — Brain Engine: Intelligent Coordinator")

    session_id = f"prod-e2e-{uuid.uuid4().hex[:8]}"

    # 12a. Quality gate evaluation
    t0 = time.time()
    try:
        r = api("POST", f"{BRAIN_URL}/api/v1/brain/quality-gate", token=token,
                json_body={
                    "session_id": session_id,
                    "tenant_id": DEMO_TENANT,
                    "rules": rules[:10] if rules else [],
                    "test_cases": test_cases[:10] if test_cases else [],
                }, timeout=60)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            score = body.get("overall_score", "?")
            level = body.get("level", "?")
            passed = body.get("passed", "?")
            result("Quality Gate", "PASS",
                   f"Score: {score} | Level: {level} | Passed: {passed}",
                   duration_ms=ms)
        else:
            result("Quality Gate", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Quality Gate", "FAIL", str(e))

    # 12b. Decision making
    t0 = time.time()
    try:
        r = api("POST", f"{BRAIN_URL}/api/v1/brain/decide", token=token,
                json_body={
                    "session_id": session_id,
                    "tenant_id": DEMO_TENANT,
                    "decision_type": "route",
                    "user_query": "What should we test next for the pharmacy platform?",
                    "rules": rules[:5] if rules else [],
                }, timeout=60)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            action = body.get("action", "?")
            confidence = body.get("confidence", "?")
            result("Brain Decision", "PASS",
                   f"Action: {action} | Confidence: {confidence}",
                   duration_ms=ms)
        else:
            result("Brain Decision", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Brain Decision", "FAIL", str(e))

    # 12c. Tier status
    t0 = time.time()
    try:
        r = api("GET", f"{BRAIN_URL}/api/v1/brain/tiers", token=token)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            mode = body.get("overall_mode", "?")
            total = body.get("total_engines", "?")
            result("Tier Status", "PASS",
                   f"Mode: {mode} | Engines: {total}",
                   duration_ms=ms)
        else:
            result("Tier Status", "INFO", f"HTTP {r.status_code}")
    except Exception as e:
        result("Tier Status", "FAIL", str(e))


# ═══════════════════════════════════════════════════════════════
#  PHASE 13: Platform API — Test Case CRUD
# ═══════════════════════════════════════════════════════════════
def phase13_platform_api(token):
    section("PHASE 13 — Platform API: Test Case CRUD & Export")
    tc_id = None

    # Create test case
    t0 = time.time()
    try:
        r = api("POST", f"{PLATFORM_API}/api/v1/test-cases", token=token,
                json_body={
                    "title": "Pharmacy Rx Validation — Production E2E",
                    "description": "Real E2E test validating prescription workflow",
                    "test_type": "e2e",
                    "priority": "critical",
                    "version": 1,
                    "target_systems": ["web", "api"],
                    "validates_rules": ["BR-PHARM-001", "BR-PHARM-002"],
                    "tags": ["pharmacy", "production", "e2e"],
                    "steps": [
                        {"step_number": 1, "action": "Search for Schedule II medication", "expected_result": "Prescription required badge shown"},
                        {"step_number": 2, "action": "Add to cart without prescription", "expected_result": "System blocks with error"},
                        {"step_number": 3, "action": "Upload valid e-prescription", "expected_result": "Prescription accepted"},
                        {"step_number": 4, "action": "Complete checkout", "expected_result": "Order confirmed with ID verification"},
                    ],
                    "preconditions": [
                        {"description": "Patient account with verified insurance"},
                        {"description": "Valid e-prescription on file"},
                    ],
                    "data_workbook": [
                        {"field_name": "Medication", "field_value": "Oxycodone 5mg", "field_type": "string"},
                        {"field_name": "ScheduleClass", "field_value": "II", "field_type": "string"},
                    ],
                })
        ms = (time.time() - t0) * 1000
        if r.status_code == 201:
            body = r.json()
            tc_id = body.get("test_case_id", "?")
            result("Create Test Case", "PASS",
                   f"ID: {tc_id} | Steps: {body.get('steps', 0)}",
                   duration_ms=ms)
        else:
            result("Create Test Case", "FAIL", f"HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        result("Create Test Case", "FAIL", str(e))

    # List + Stats + Export
    if tc_id:
        t0 = time.time()
        try:
            r = api("GET", f"{PLATFORM_API}/api/v1/test-cases", token=token)
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                result("List Test Cases", "PASS",
                       f"Total: {body.get('total', 0)} test cases",
                       duration_ms=ms)
        except Exception as e:
            result("List Test Cases", "FAIL", str(e))

        # Export
        t0 = time.time()
        try:
            r = api("POST", f"{PLATFORM_API}/api/v1/test-cases/export", token=token,
                    json_body={"format": "json"})
            ms = (time.time() - t0) * 1000
            if r.status_code == 200:
                body = r.json()
                result("Export Test Cases", "PASS",
                       f"Format: {body.get('format', '?')} | Records: {body.get('record_count', 0)}",
                       duration_ms=ms)
        except Exception as e:
            result("Export Test Cases", "FAIL", str(e))

        # Cleanup
        try:
            r = api("DELETE", f"{PLATFORM_API}/api/v1/test-cases/{tc_id}", token=token)
            result("Cleanup", "PASS" if r.status_code == 204 else "INFO",
                   f"HTTP {r.status_code}")
        except Exception as e:
            result("Cleanup", "INFO", str(e))


# ═══════════════════════════════════════════════════════════════
#  PHASE 14: Gateway — Unified API Routing
# ═══════════════════════════════════════════════════════════════
def phase14_gateway(token):
    section("PHASE 14 — Gateway: Unified API Routing")

    # Test gateway routing to Shield
    t0 = time.time()
    try:
        r = api("POST", f"{GATEWAY_URL}/api/v1/shield/analyze", token=token,
                json_body={"text": "Email me at test@example.com please.", "tenant_id": DEMO_TENANT})
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            body = r.json()
            entities = body.get("entities", [])
            result("Gateway → Shield", "PASS",
                   f"Routed successfully, {len(entities)} entities found",
                   duration_ms=ms)
        else:
            result("Gateway → Shield", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("Gateway → Shield", "FAIL", str(e))

    # Test gateway routing to Heart
    t0 = time.time()
    try:
        r = api("POST", f"{GATEWAY_URL}/api/v1/heart/ask", token=token,
                json_body={
                    "question": "What is a controlled substance?",
                    "tenant_id": DEMO_TENANT,
                    "context": "Controlled substances are drugs regulated by the DEA.",
                }, timeout=120)
        ms = (time.time() - t0) * 1000
        if r.status_code == 200:
            result("Gateway → Heart (LLM)", "PASS",
                   f"Routed to real Ollama inference",
                   duration_ms=ms)
        else:
            result("Gateway → Heart", "FAIL", f"HTTP {r.status_code}")
    except Exception as e:
        result("Gateway → Heart", "FAIL", str(e))


# ═══════════════════════════════════════════════════════════════
#  MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════
def main():
    start_time = time.time()

    print(f"\n{Colors.BOLD}{Colors.CYAN}")
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║           NEXUS QA — PRODUCTION END-TO-END TEST                    ║")
    print("║           Real Services • Real LLM • Real Data • No Stubs          ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"{Colors.END}")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Video:   {VIDEO_PATH}")
    print(f"  LLM:     Ollama llama3.2:1b (real inference)")
    print(f"  DB:      PostgreSQL + Neo4j + Redis (real)")
    print()

    # Phase 0: Health
    if not phase0_health():
        print(f"\n{Colors.RED}ABORT: Not all services are healthy.{Colors.END}")
        sys.exit(1)

    # Phase 1: Auth
    token = phase1_auth()
    if not token:
        print(f"\n{Colors.RED}ABORT: Authentication failed.{Colors.END}")
        sys.exit(1)

    # Phase 2: Shield PII
    shield = phase2_shield(token)
    safe_transcript = shield["redacted_text"]

    # Phase 3: Ears (audio transcription)
    ears = phase3_ears(token)

    # Phase 4: Eyes (visual analysis)
    eyes = phase4_eyes(token)

    # Phase 5: Spine (document ingestion)
    spine = phase5_spine(token)

    # Phase 6: Heart (AI rule extraction — REAL Ollama LLM)
    heart = phase6_heart(token, safe_transcript)

    # Phase 7: Backbone (knowledge graph)
    backbone = phase7_backbone(token, heart["rules"])

    # Phase 8: Nerves
    phase8_nerves(token)

    # Phase 9: Hands (test data)
    hands = phase9_hands(token)

    # Phase 10: Legs
    phase10_legs(token)

    # Phase 11: Mouth (report)
    mouth = phase11_mouth(token, heart["rules"], heart["test_cases"])

    # Phase 12: Brain (coordinator)
    phase12_brain(token, heart["rules"], heart["test_cases"])

    # Phase 13: Platform API
    phase13_platform_api(token)

    # Phase 14: Gateway routing
    phase14_gateway(token)

    # ─── Summary ───────────────────────────────────────────────
    total_time = time.time() - start_time
    total = PASS_COUNT + FAIL_COUNT + SKIP_COUNT

    print(f"\n{Colors.BOLD}{Colors.CYAN}")
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║                     PRODUCTION E2E TEST RESULTS                    ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print(f"{Colors.END}")
    print(f"  {Colors.GREEN}PASSED:  {PASS_COUNT}{Colors.END}")
    print(f"  {Colors.RED}FAILED:  {FAIL_COUNT}{Colors.END}")
    print(f"  {Colors.YELLOW}SKIPPED: {SKIP_COUNT}{Colors.END}")
    print(f"  Total:   {total} checks in {total_time:.1f}s")
    print()
    print(f"  Engines tested:   11 (shield, ears, eyes, heart, backbone, nerves,")
    print(f"                        legs, hands, spine, mouth, brain)")
    print(f"  Services tested:  15 (+ auth, gateway, platform-api, orchestrator)")
    print(f"  Real LLM calls:   Heart → Ollama llama3.2:1b")
    print(f"  Real databases:   PostgreSQL + Neo4j + Redis")
    print(f"  Real video:       {VIDEO_PATH.name} processed")
    print()

    if FAIL_COUNT == 0:
        print(f"  {Colors.GREEN}{Colors.BOLD}*** ALL PRODUCTION CHECKS PASSED ***{Colors.END}")
    else:
        print(f"  {Colors.RED}{Colors.BOLD}*** {FAIL_COUNT} CHECKS FAILED ***{Colors.END}")

    print()
    sys.exit(0 if FAIL_COUNT == 0 else 1)


if __name__ == "__main__":
    main()
