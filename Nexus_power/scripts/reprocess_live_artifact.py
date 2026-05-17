"""Re-process the live artifact through spine to regenerate flows/edges/controls."""
import json
import sys
import httpx

GATEWAY = "http://localhost:8080"
SPINE = "http://localhost:8009"
ARTIFACT_ID = "494cb0e6-897d-4e62-b63b-bec63a4ff634"
SESSION_ID = "75b636f4-4976-4585-a878-00c4052f27cd"
TENANT_ID = "nexus-platform"
FRAMES_FILE = "C:/Users/harik/nexusqa/frames_payload_raw.json"

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
print(f"Authenticated OK")

# Load frames from JSONL (one JSON object per line)
frames = []
with open(FRAMES_FILE, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            frames.append(json.loads(line))
print(f"Loaded {len(frames)} frames from JSONL")

# Submit to spine
body = {
    "tenant_id": TENANT_ID,
    "session_id": SESSION_ID,
    "artifact_id": ARTIFACT_ID,
    "frames": frames,
}
print(f"Submitting to spine at {SPINE}/api/v1/spine/persist-visual-frames ...")
r = httpx.post(
    f"{SPINE}/api/v1/spine/persist-visual-frames",
    json=body,
    headers=headers,
    timeout=120.0,
)
print(f"Status: {r.status_code}")
resp = r.json()
print(json.dumps(resp, indent=2))
