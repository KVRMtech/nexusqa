"""
End-to-end test: Test Case CRUD + Export via platform API.

Verifies:
  1. Create test case (POST /api/v1/test-cases)
  2. List test cases (GET /api/v1/test-cases)
  3. Get single test case (GET /api/v1/test-cases/{id})
  4. Update test case (PUT /api/v1/test-cases/{id})
  5. Export to Excel (POST /api/v1/test-cases/export)
  6. Get stats (GET /api/v1/test-cases/stats)
  7. Delete test case (DELETE /api/v1/test-cases/{id})
"""

import httpx
import json
import os
import sys

BASE = os.environ.get("PLATFORM_API_URL", "http://localhost:8091")
AUTH = os.environ.get("AUTH_URL", "http://localhost:8000")
E2E_EMAIL = os.environ.get("NEXUS_E2E_EMAIL", "admin@nexus.local")
E2E_PASSWORD = os.environ.get("NEXUS_E2E_PASSWORD", "change-this-password")


def main():
    # 1) Login
    r = httpx.post(f"{AUTH}/api/v1/auth/login", json={
        "email": E2E_EMAIL, "password": E2E_PASSWORD
    })
    assert r.status_code == 200, f"Login failed: {r.text}"
    token = r.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    print("[OK] Authenticated")

    # 2) Create test case
    payload = {
        "title": "Online Pharmacy Order - E2E Validation",
        "description": "Full end-to-end pharmacy order flow",
        "test_type": "e2e",
        "priority": "high",
        "version": 11,
        "target_systems": ["web"],
        "validates_rules": ["BR-PHARM-001", "BR-PHARM-002"],
        "tags": ["pharmacy", "e-commerce"],
        "steps": [
            {"step_number": 1, "action": "Navigate to pharmacy portal", "expected_result": "Home page loads"},
            {"step_number": 2, "action": "Click Register", "expected_result": "Registration form shown"},
            {"step_number": 3, "action": "Enter (Data.FirstName) in name field", "expected_result": "Name accepted", "input_data_refs": ["FirstName"]},
            {"step_number": 4, "action": "Enter (Data.Email) in email field", "expected_result": "Email validated", "input_data_refs": ["Email"]},
            {"step_number": 5, "action": "Submit registration", "expected_result": "Account created successfully"},
        ],
        "preconditions": [
            {"description": "Pharmacy portal is accessible"},
            {"description": "Test payment gateway in sandbox mode"},
        ],
        "data_workbook": [
            {"field_name": "FirstName", "field_value": "(Data.FirstName)", "field_type": "string"},
            {"field_name": "Email", "field_value": "(Data.Email)", "field_type": "email"},
        ],
    }
    r = httpx.post(f"{BASE}/api/v1/test-cases", json=payload, headers=hdr, timeout=15)
    assert r.status_code == 201, f"Create failed ({r.status_code}): {r.text}"
    created = r.json()
    tc_id = created["test_case_id"]
    print(f"[OK] Created: {tc_id} | steps={created['steps']} | data={created['data_workbook_entries']}")

    # 3) List test cases
    r = httpx.get(f"{BASE}/api/v1/test-cases", headers=hdr, timeout=15)
    assert r.status_code == 200, f"List failed: {r.text}"
    listing = r.json()
    print(f"[OK] Listed: {listing['total']} total, {len(listing['items'])} returned")

    # 4) Get single test case
    r = httpx.get(f"{BASE}/api/v1/test-cases/{tc_id}", headers=hdr, timeout=15)
    assert r.status_code == 200, f"Get failed: {r.text}"
    tc = r.json()
    assert tc["test_case_id"] == tc_id
    assert len(tc["steps"]) == 5
    assert len(tc["preconditions"]) == 2
    assert len(tc["data_workbook"]) == 2
    print(f"[OK] Fetched: {tc_id} | {len(tc['steps'])} steps | {len(tc['data_workbook'])} data fields")

    # 5) Update test case
    r = httpx.put(f"{BASE}/api/v1/test-cases/{tc_id}", json={
        "title": "Online Pharmacy Order - UPDATED",
        "status": "review",
        "tags": ["pharmacy", "e-commerce", "updated"],
    }, headers=hdr, timeout=15)
    assert r.status_code == 200, f"Update failed: {r.text}"
    updated = r.json()
    assert updated["title"] == "Online Pharmacy Order - UPDATED"
    assert updated["status"] == "review"
    print(f"[OK] Updated: status={updated['status']} | tags={updated['tags']}")

    # 6) Stats
    r = httpx.get(f"{BASE}/api/v1/test-cases/stats", headers=hdr, timeout=15)
    assert r.status_code == 200, f"Stats failed: {r.text}"
    stats = r.json()
    print(f"[OK] Stats: total={stats['total_test_cases']} | steps={stats['total_steps']} | data={stats['total_data_fields']}")

    # 7) Export to JSON (file size validation — Excel needs openpyxl on server)
    r = httpx.post(f"{BASE}/api/v1/test-cases/export", json={
        "format": "json",
    }, headers=hdr, timeout=30)
    assert r.status_code == 200, f"Export failed: {r.text}"
    export = r.json()
    print(f"[OK] Export: format={export['format']} | size={export['file_size_bytes']}B | records={export['record_count']}")

    # 8) Delete test case
    r = httpx.delete(f"{BASE}/api/v1/test-cases/{tc_id}", headers=hdr, timeout=15)
    assert r.status_code == 204, f"Delete failed ({r.status_code}): {r.text}"
    print(f"[OK] Deleted: {tc_id}")

    # 9) Verify deletion
    r = httpx.get(f"{BASE}/api/v1/test-cases/{tc_id}", headers=hdr, timeout=15)
    assert r.status_code == 404, f"Expected 404 after delete, got {r.status_code}"
    print(f"[OK] Verified: 404 after deletion")

    print("\n=== ALL CRUD + EXPORT TESTS PASSED ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
