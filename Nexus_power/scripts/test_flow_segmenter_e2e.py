"""Quick e2e test for Phase 5b flow segmenter integration."""
import json
import subprocess
import sys

import requests

GATEWAY = "http://localhost:8080"
ARTIFACT = "c62dadf3-c319-473d-bf16-7f974e04bb0a"
SESSION = "2fe19e03-e58a-4596-9d44-6dddef3f4bd9"
TENANT = "nexus-platform"

# 1. Login
r = requests.post(f"{GATEWAY}/api/v1/auth/login", json={
    "email": "admin@nexus.local", "password": "admin123",
})
r.raise_for_status()
token = r.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}
print(f"[OK] Logged in")

# 2. Extract existing frames from DB
result = subprocess.run(
    ["docker", "compose", "-f", "docker-compose.yml", "exec", "-T", "postgres",
     "psql", "-U", "nexus", "-d", "nexus", "-t", "-A", "-c",
     f"SELECT json_agg(json_build_object("
     f"'frame_id',frame_id,"
     f"'frame_index',frame_index,"
     f"'timestamp_seconds',timestamp_seconds,"
     f"'frame_path',frame_path,"
     f"'frame_asset_path',COALESCE(frame_asset_path,''),"
     f"'application_type',application_type,"
     f"'page_title',page_title,"
     f"'url_or_path',url_or_path,"
     f"'ui_elements',ui_elements_json,"
     f"'extracted_text',extracted_text,"
     f"'description',description,"
     f"'ocr_confidence',ocr_confidence,"
     f"'is_keyframe',is_keyframe,"
     f"'job_id',job_id,"
     f"'video_id',COALESCE(video_id,'')"
     f")) FROM visual_frames WHERE artifact_id='{ARTIFACT}'"],
    capture_output=True, text=True,
)
frames = json.loads(result.stdout.strip())
print(f"[OK] Extracted {len(frames)} frames from DB")
for f in frames[:3]:
    print(f"     idx={f['frame_index']} app={f['application_type']} url={f.get('url_or_path','')[:60]}")

# 3. Clear stale data so ON CONFLICT DO NOTHING doesn't skip new flows
subprocess.run(
    ["docker", "compose", "-f", "docker-compose.yml", "exec", "-T", "postgres",
     "psql", "-U", "nexus", "-d", "nexus", "-c",
     f"DELETE FROM visual_flows WHERE artifact_id='{ARTIFACT}';"
     f"DELETE FROM evidence_controls WHERE artifact_id='{ARTIFACT}';"
     f"DELETE FROM visual_flow_edges WHERE artifact_id='{ARTIFACT}';"
     f"UPDATE visual_scenes SET flow_id=NULL WHERE artifact_id='{ARTIFACT}';"
     f"DELETE FROM visual_scenes WHERE artifact_id='{ARTIFACT}';"
     f"DELETE FROM app_instances WHERE artifact_id='{ARTIFACT}';"],
    capture_output=True, text=True,
)
print("[OK] Cleared stale scenes/flows/controls/edges")

# 4. Re-submit frames to trigger full pipeline
body = {
    "tenant_id": TENANT,
    "session_id": SESSION,
    "artifact_id": ARTIFACT,
    "frames": frames,
}
r = requests.post(f"{GATEWAY}/api/v1/spine/persist-visual-frames",
                   json=body, headers=headers)
r.raise_for_status()
resp = r.json()
print(f"\n=== Pipeline Result ===")
print(json.dumps(resp, indent=2))

# 5. Verify DB state
for query, label in [
    (f"SELECT count(*) FROM visual_flows WHERE artifact_id='{ARTIFACT}'", "visual_flows"),
    (f"SELECT count(*) FROM visual_scenes WHERE flow_id IS NOT NULL AND artifact_id='{ARTIFACT}'", "scenes with flow_id"),
    (f"SELECT count(*) FROM visual_flow_edges WHERE flow_id IS NOT NULL AND artifact_id='{ARTIFACT}'", "edges with flow_id"),
    (f"SELECT flow_id, flow_label, domain, app_type, scene_count, is_noise FROM visual_flows WHERE artifact_id='{ARTIFACT}'", "flow details"),
]:
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "exec", "-T", "postgres",
         "psql", "-U", "nexus", "-d", "nexus", "-c", query],
        capture_output=True, text=True,
    )
    print(f"\n[{label}]")
    print(result.stdout.strip())

success = resp.get("flows_persisted", 0) > 0
print(f"\n{'PASS' if success else 'FAIL'}: flows_persisted={resp.get('flows_persisted',0)}")
sys.exit(0 if success else 1)
