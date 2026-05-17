"""
Nexus QA — Start All Backend Services
Run from nexus-qa root:
  python scripts/start_all.py
"""

import subprocess
import sys
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "Scripts", "python.exe")

SERVICES = [
    # (name, script_path, port)
    ("auth",     "platform/auth-service/main.py",   8000),
    ("gateway",  "platform/gateway/main.py",        8080),
    ("shield",   "engines/shield-engine/main.py",   8001),
    ("ears",     "engines/ears-engine/main.py",      8002),
    ("eyes",     "engines/eyes-engine/main.py",      8003),
    ("heart",    "engines/heart-engine/main.py",     8004),
    ("backbone", "engines/backbone-engine/main.py",  8005),
    ("nerves",   "engines/nerves-engine/main.py",    8006),
    ("legs",     "engines/legs-engine/main.py",      8007),
    ("hands",    "engines/hands-engine/main.py",     8008),
    ("spine",    "engines/spine-engine/main.py",     8009),
    ("mouth",    "engines/mouth-engine/main.py",     8010),
    ("brain",    "engines/brain-engine/main.py",     8011),
]

def main():
    os.chdir(ROOT)
    procs = []
    
    for name, script, port in SERVICES:
        script_path = os.path.join(ROOT, script)
        if not os.path.exists(script_path):
            print(f"  [SKIP] {name}: {script} not found")
            continue
        
        print(f"  [START] {name} on port {port}...")
        env = os.environ.copy()
        env["ENGINE_PORT"] = str(port)
        
        proc = subprocess.Popen(
            [PYTHON, script_path],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append((name, port, proc))
        time.sleep(1)  # stagger startup
    
    print(f"\n  All {len(procs)} services launched. Waiting 5s for startup...")
    time.sleep(5)
    
    # Check health
    import httpx
    alive = 0
    for name, port, proc in procs:
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode()[-500:] if proc.stderr else ""
            print(f"  [DEAD] {name}:{port} — exited with code {proc.returncode}")
            if stderr:
                print(f"         {stderr[:200]}")
            continue
        
        try:
            r = httpx.get(f"http://localhost:{port}/", timeout=3)
            if r.status_code == 200:
                print(f"  [OK]   {name}:{port}")
                alive += 1
            else:
                print(f"  [WARN] {name}:{port} — HTTP {r.status_code}")
        except Exception:
            print(f"  [WAIT] {name}:{port} — not responding yet (process running)")
            alive += 1  # process is alive, just slow to start
    
    print(f"\n  Result: {alive}/{len(procs)} services running")
    
    if alive > 0:
        print("\n  Press Ctrl+C to stop all services...\n")
        try:
            while True:
                time.sleep(1)
                # Check if any died
                for name, port, proc in procs:
                    if proc.poll() is not None:
                        pass  # already dead
        except KeyboardInterrupt:
            print("\n  Shutting down...")
            for name, port, proc in procs:
                if proc.poll() is None:
                    proc.terminate()
            print("  Done.")

if __name__ == "__main__":
    main()
