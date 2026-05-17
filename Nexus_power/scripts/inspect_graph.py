"""Inspect the visual evidence graph API response."""
import httpx, json

auth = httpx.post('http://localhost:8080/api/v1/auth/login',
                  json={'email': 'admin@nexus.local', 'password': 'admin123'})
token = auth.json()['access_token']
h = {'Authorization': f'Bearer {token}'}

r = httpx.get('http://localhost:8091/api/v1/artifacts/c62dadf3-c319-473d-bf16-7f974e04bb0a/visual-evidence-graph',
              headers=h, timeout=30)
d = r.json()

print(f"Status: {r.status_code}")
print(f"Flows: {len(d.get('flows', []))}")
print(f"Scenes: {len(d.get('scenes', []))}")
print(f"Edges: {len(d.get('edges', []))}")
print(f"Summary: {json.dumps(d.get('summary', {}), indent=2)}")
print()

for f in d.get('flows', []):
    label = f.get('flow_label', '')[:80]
    conf = f.get('confidence', 0)
    fid = f.get('flow_id', '')[:8]
    print(f"Flow {f['flow_index']}: [{fid}] \"{label}\" confidence={conf}")
print()

for s in d.get('scenes', []):
    sid = s.get('scene_id', '')[:8]
    app = s.get('application_type', '?')
    title = s.get('page_title', '')[:50]
    desc = s.get('description', '')[:60]
    fid = s.get('flow_id', '')[:8]
    frame = s.get('representative_frame_asset_path', '')[:50]
    ctrls = d.get('controls_by_scene', {}).get(s.get('scene_id', ''), [])
    ctrl_types = [c.get('element_type', '') for c in ctrls]
    print(f"Scene {s['scene_index']}: [{sid}] app={app} title=\"{title}\"")
    print(f"  desc=\"{desc}\"")
    print(f"  flow={fid} frame_asset=\"{frame}\"")
    print(f"  controls={len(ctrls)}: {ctrl_types}")
print()

for e in d.get('edges', []):
    frm = e.get('from_scene_id', '')[:8]
    to = e.get('to_scene_id', '')[:8]
    etype = e.get('edge_type', '')
    action = e.get('action_type', '')
    val = e.get('action_value', '')[:60]
    conf = e.get('evidence_confidence', 0)
    print(f"Edge: {frm}..-> {to}.. type={etype} action={action} value=\"{val}\" conf={conf}")
