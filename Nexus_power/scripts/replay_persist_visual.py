"""Replay the persist_visual_evidence stage for a specific artifact.

Usage (from Nexus_power/):
    docker exec nexus-orchestrator python /app/scripts/replay_persist_visual.py

Hardcoded for the failed artifact from session 0bab1353.
"""
import asyncio
import httpx

GATEWAY = "http://nexus-gateway:8080"
EYES_JOB = "fb426710-f3e0-49f9-a95b-0eff5bdce2ae"
ARTIFACT_ID = "8e1b7e47-39f8-46a8-9f30-8c10e363e557"
SESSION_ID = "0bab1353-41fc-4179-91b0-38ba5bb8970b"
TENANT_ID = "nexus-platform"


async def main():
    async with httpx.AsyncClient(base_url=GATEWAY, timeout=120.0) as c:
        # Authenticate
        r = await c.post(
            "/api/v1/auth/login",
            json={"email": "admin@nexus.local", "password": "admin123"},
        )
        tok = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}

        # Fetch frames from Eyes
        eyes_r = await c.get(f"/api/v1/eyes/jobs/{EYES_JOB}", headers=headers)
        frames = eyes_r.json().get("result", {}).get("frames", [])
        print(f"Frames from Eyes job: {len(frames)}")

        # Call Spine persist-visual-frames
        payload = {
            "tenant_id": TENANT_ID,
            "session_id": SESSION_ID,
            "artifact_id": ARTIFACT_ID,
            "frames": frames,
        }
        r2 = await c.post(
            "/api/v1/spine/persist-visual-frames",
            json=payload,
            headers=headers,
        )
        data = r2.json()
        print(f"HTTP {r2.status_code}")
        print(f"  success        = {data.get('success')}")
        print(f"  frames         = {data.get('frames_persisted')}")
        print(f"  scenes         = {data.get('scenes_persisted')}")
        print(f"  controls       = {data.get('controls_persisted')}")
        print(f"  flow_edges     = {data.get('flow_edges_persisted')}")
        print(f"  flows          = {data.get('flows_persisted')}")
        errors = data.get("errors") or []
        if errors:
            print(f"  errors         = {errors}")


if __name__ == "__main__":
    asyncio.run(main())
