"""Re-process test artifact through spine to regenerate flows/edges/controls."""
import json
import sys
import httpx

GATEWAY = "http://localhost:8080"
SPINE = "http://localhost:8009"
ARTIFACT_ID = "c62dadf3-c319-473d-bf16-7f974e04bb0a"
SESSION_ID = "2fe19e03-e58a-4596-9d44-6dddef3f4bd9"
TENANT_ID = "nexus-platform"

# Login
auth = httpx.post(f"{GATEWAY}/api/v1/auth/login", json={
    "email": "admin@nexus.local",
    "password": "admin123",
})
token = auth.json().get("access_token", "")
if not token:
    print("Auth failed:", auth.text[:200])
    sys.exit(1)
headers = {"Authorization": f"Bearer {token}"}

# Load frames
with open("data/frames_export.json", encoding="utf-16") as f:
    raw = f.read().strip()
    frames = json.loads(raw)
print(f"Loaded {len(frames)} frames")

# Submit to spine
body = {
    "tenant_id": TENANT_ID,
    "session_id": SESSION_ID,
    "artifact_id": ARTIFACT_ID,
    "frames": frames,
}
r = httpx.post(
    f"{SPINE}/api/v1/spine/persist-visual-frames",
    json=body,
    headers=headers,
    timeout=60.0,
)
print(f"Status: {r.status_code}")
resp = r.json()
print(json.dumps(resp, indent=2))
