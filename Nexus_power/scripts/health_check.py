"""Quick health check for all 15 Nexus QA services."""
import httpx

SERVICES = [
    ("auth-service", 8000),
    ("shield", 8001),
    ("ears", 8002),
    ("eyes", 8003),
    ("heart", 8004),
    ("backbone", 8005),
    ("nerves", 8006),
    ("legs", 8007),
    ("hands", 8008),
    ("spine", 8009),
    ("mouth", 8010),
    ("brain", 8011),
    ("gateway", 8080),
    ("platform-api", 8091),
    ("orchestrator", 8100),
]

up = 0
for name, port in SERVICES:
    try:
        r = httpx.get(f"http://localhost:{port}/health", timeout=3)
        if r.status_code == 200:
            print(f"  {name:16s} :{port}  UP")
            up += 1
        else:
            print(f"  {name:16s} :{port}  HTTP {r.status_code}")
    except Exception as e:
        print(f"  {name:16s} :{port}  DOWN ({type(e).__name__})")

print(f"\n  {up}/15 services healthy")
