."""Quick end-to-end test through the Vite proxy → Gateway → Engines."""
import httpx

BASE = "http://127.0.0.1:8080"

print("=== Nexus QA End-to-End Test ===\n")

# 1. Login
print("[1] Login...")
r = httpx.post(f"{BASE}/api/v1/auth/login", json={
    "email": "admin@nexus.local",
    "password": "admin123",
}, timeout=15)
assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
data = r.json()
token = data["access_token"]
user = data["user"]
print(f"    OK — {user['email']} ({user['role']}), tenant={user['tenant_id']}")
headers = {"Authorization": f"Bearer {token}"}

# 2. Engine health checks
print("\n[2] Engine health checks (via gateway)...")
engines = ["shield", "ears", "eyes", "heart", "backbone", "nerves", "legs", "hands", "spine", "mouth"]
for eng in engines:
    try:
        h = httpx.get(f"{BASE}/api/v1/{eng}/health/ready", headers=headers, timeout=8)
        status = h.json().get("status", "?")
        print(f"    {eng:10s} → {status}")
    except Exception as e:
        print(f"    {eng:10s} → ERROR: {str(e)[:60]}")

# 3. Shield PII redaction
print("\n[3] Shield PII Redaction...")
r = httpx.post(f"{BASE}/api/v1/shield/redact", json={
    "text": "My name is John Smith and my SSN is 123-45-6789. Email: john@test.com",
    "tenant_id": "nexus-platform",
    "trace_id": "e2e-test-1",
}, headers=headers, timeout=15)
if r.status_code == 200:
    result = r.json()
    print(f"    OK — found {result.get('pii_count', '?')} PII entities")
    print(f"    Safe text: {result.get('safe_text', result.get('redacted_text', ''))[:80]}")
else:
    print(f"    FAILED: {r.status_code} {r.text[:100]}")

# 4. Backbone Knowledge Graph stats
print("\n[4] Backbone Graph Stats...")
try:
    r = httpx.get(f"{BASE}/api/v1/backbone/stats", headers=headers, timeout=15)
    if r.status_code == 200:
        print(f"    OK — {r.json()}")
    else:
        print(f"    FAILED: {r.status_code} {r.text[:100]}")
except Exception as e:
    print(f"    TIMEOUT: {str(e)[:60]}")

# 5. Nerves list connectors
print("\n[5] Nerves Connectors...")
try:
    r = httpx.get(f"{BASE}/api/v1/nerves/connectors", headers=headers, timeout=10)
    if r.status_code == 200:
        connectors = r.json()
        if isinstance(connectors, list):
            print(f"    OK — {len(connectors)} connectors available")
        elif isinstance(connectors, dict):
            print(f"    OK — {connectors}")
    else:
        print(f"    FAILED: {r.status_code} {r.text[:100]}")
except Exception as e:
    print(f"    TIMEOUT: {str(e)[:60]}")

# 6. Mouth reports list
print("\n[6] Mouth Reports...")
try:
    r = httpx.get(f"{BASE}/api/v1/mouth/reports?tenant_id=nexus-platform", headers=headers, timeout=10)
    if r.status_code == 200:
        print(f"    OK — {r.json()}")
    else:
        print(f"    FAILED: {r.status_code} {r.text[:100]}")
except Exception as e:
    print(f"    TIMEOUT: {str(e)[:60]}")

print("\n=== End-to-End Test Complete ===")
