"""
Nexus QA Platform — Locust Load Test Suite

Covers all major API endpoints across services:
  - Platform API (port 8091): Sessions, Insights, Admin, Guardrails
  - Auth Service (port 8000): Login, Token validation
  - Gateway (port 8080): Proxied routes

Usage:
  locust -f tests/load/locustfile.py --host http://localhost:8091
  locust -f tests/load/locustfile.py --host http://localhost:8091 --headless -u 100 -r 10 -t 5m

Targets:
  - 95th percentile response < 500ms under 100 concurrent users
  - Zero 5xx errors
  - Throughput > 200 req/s
"""
from __future__ import annotations

import json
import random
import string
import uuid
from locust import HttpUser, TaskSet, task, between, events  # type: ignore[import-not-found]


# ─── Helpers ───────────────────────────────────────────────────

def _random_id() -> str:
    return str(uuid.uuid4())


def _random_text(length: int = 50) -> str:
    return "".join(random.choices(string.ascii_lowercase + " ", k=length)).strip()


# ─── Platform API Tasks ───────────────────────────────────────

class InsightsTaskSet(TaskSet):
    """Executive Insights module — aggregation-heavy SQL queries."""

    @task(5)
    def get_kpis(self):
        self.client.get("/api/v1/insights/kpis?tenant_id=t-1", name="/insights/kpis")

    @task(3)
    def get_roi(self):
        self.client.get("/api/v1/insights/roi?tenant_id=t-1", name="/insights/roi")

    @task(3)
    def get_risks(self):
        self.client.get("/api/v1/insights/risks?tenant_id=t-1", name="/insights/risks")

    @task(2)
    def get_weekly_trend(self):
        self.client.get("/api/v1/insights/weekly-trend?tenant_id=t-1", name="/insights/weekly-trend")

    @task(4)
    def get_engine_status(self):
        """Hits all 10 engine health endpoints — tests Redis caching."""
        self.client.get("/api/v1/insights/engines", name="/insights/engines")


class SessionsTaskSet(TaskSet):
    """Knowledge Capture sessions — CRUD operations."""

    @task(5)
    def list_sessions(self):
        self.client.get("/api/v1/sessions?tenant_id=t-1", name="/sessions [list]")

    @task(2)
    def create_session(self):
        payload = {
            "sme_name": f"LoadTest-{_random_text(10)}",
            "domain": random.choice(["banking", "insurance", "healthcare", "retail"]),
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/sessions",
            json=payload,
            name="/sessions [create]",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 201, 422):
                resp.success()

    @task(3)
    def get_session_detail(self):
        """Get a single session — may 404 if no sessions exist."""
        with self.client.get(
            f"/api/v1/sessions/{_random_id()}",
            name="/sessions/{id}",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404):
                resp.success()


class AdminTaskSet(TaskSet):
    """Admin module — engine grid, resources, integrations."""

    @task(4)
    def get_admin_engines(self):
        """Tests Redis-cached engine health aggregation."""
        self.client.get("/api/v1/admin/engines", name="/admin/engines")

    @task(3)
    def get_admin_resources(self):
        """Tests Redis-cached system resources (psutil)."""
        self.client.get("/api/v1/admin/resources", name="/admin/resources")

    @task(2)
    def get_admin_integrations(self):
        self.client.get("/api/v1/admin/integrations?tenant_id=t-1", name="/admin/integrations")

    @task(2)
    def get_admin_audit(self):
        self.client.get("/api/v1/admin/audit?tenant_id=t-1", name="/admin/audit")

    @task(3)
    def get_admin_users(self):
        self.client.get("/api/v1/admin/users?tenant_id=t-1", name="/admin/users")


class GuardrailsTaskSet(TaskSet):
    """AI Confidence Guardrails — pipeline monitoring."""

    @task(4)
    def list_pipeline(self):
        self.client.get("/api/v1/guardrails/pipeline?tenant_id=t-1", name="/guardrails/pipeline")

    @task(3)
    def list_review_queue(self):
        self.client.get("/api/v1/guardrails/review-queue?tenant_id=t-1", name="/guardrails/review-queue")


class TraceabilityTaskSet(TaskSet):
    """Living Traceability Matrix — cross-reference lookups."""

    @task(4)
    def list_traces(self):
        self.client.get("/api/v1/traceability/traces?tenant_id=t-1", name="/traceability/traces")


class TestManagementTaskSet(TaskSet):
    """Test management — suites and runs."""

    @task(4)
    def list_suites(self):
        self.client.get("/api/v1/tests/suites?tenant_id=t-1", name="/tests/suites")

    @task(3)
    def list_runs(self):
        self.client.get("/api/v1/tests/runs?tenant_id=t-1", name="/tests/runs")


class ContradictionsTaskSet(TaskSet):
    """Contradiction tracking."""

    @task(4)
    def list_contradictions(self):
        self.client.get("/api/v1/contradictions?tenant_id=t-1", name="/contradictions")


class HealthCheckTaskSet(TaskSet):
    """Quick health probes — should be sub-10ms."""

    @task(10)
    def health(self):
        self.client.get("/health", name="/health")


# ─── User Profiles ─────────────────────────────────────────────

class PlatformAPIUser(HttpUser):
    """
    Simulates a typical Nexus user browsing the dashboard.
    
    Weighted task distribution models real usage:
      - Insights (dashboard) gets heaviest traffic
      - Admin/Health are frequent background polls
      - CRUD operations are less frequent
    """
    wait_time = between(0.5, 2.0)

    tasks = {
        InsightsTaskSet: 5,       # Dashboard is most-visited
        SessionsTaskSet: 3,       # Session CRUD is common
        AdminTaskSet: 3,          # Admin panel polls frequently
        GuardrailsTaskSet: 2,     # Guardrails monitoring
        TraceabilityTaskSet: 2,   # Traceability lookups
        TestManagementTaskSet: 2, # Test management
        ContradictionsTaskSet: 1, # Contradiction tracking
        HealthCheckTaskSet: 1,    # Health probes
    }


class HeavyAPIUser(HttpUser):
    """
    Simulates an API consumer making rapid back-to-back calls.
    Used for stress testing — shorter wait times.
    """
    wait_time = between(0.1, 0.5)
    weight = 1  # 1 heavy user per 3 normal users (default weight=1)

    tasks = {
        InsightsTaskSet: 4,
        AdminTaskSet: 4,
        SessionsTaskSet: 2,
    }


# ─── Auth Service Load Test ───────────────────────────────────

class AuthServiceUser(HttpUser):
    """
    Tests auth service directly (port 8000).
    Requires --host http://localhost:8000

    Usage: locust -f tests/load/locustfile.py AuthServiceUser --host http://localhost:8000
    """
    wait_time = between(0.5, 1.5)
    weight = 0  # Disabled by default — enable via CLI

    @task(5)
    def health(self):
        self.client.get("/health", name="[auth] /health")

    @task(3)
    def login(self):
        with self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "admin"},
            name="[auth] /login",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 401, 422):
                resp.success()


# ─── Engine Load Tests ─────────────────────────────────────────

class ShieldTaskSet(TaskSet):
    """Shield engine (port 8001) — PII redaction."""

    @task(5)
    def redact_text(self):
        payload = {
            "text": f"John Smith called from 555-{random.randint(1000, 9999)} about policy ABC123. SSN 123-45-6789.",
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/shield/redact",
            json=payload,
            name="[shield] /redact",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(3)
    def detect_pii(self):
        payload = {
            "text": "My email is test@example.com and my phone is 555-0100.",
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/shield/detect",
            json=payload,
            name="[shield] /detect",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(2)
    def get_audit_log(self):
        self.client.get(
            "/api/v1/shield/audit?tenant_id=t-1",
            name="[shield] /audit",
        )

    @task(4)
    def health(self):
        self.client.get("/health", name="[shield] /health")


class HeartTaskSet(TaskSet):
    """Heart engine (port 8004) — AI rule extraction."""

    @task(3)
    def extract_rules(self):
        payload = {
            "transcript": "If the applicant is over 35 and smokes, the premium is 1.75x base rate. Non-resident aliens need I-94 and W-8BEN forms.",
            "session_id": _random_id(),
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/heart/extract-rules",
            json=payload,
            name="[heart] /extract-rules",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(2)
    def generate_tests(self):
        payload = {
            "rules": [
                {
                    "description": "Premium depends on age band",
                    "condition": "IF age >= 35",
                    "expected_result": "THEN rate = base * 1.5",
                }
            ],
            "session_id": _random_id(),
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/heart/generate-tests",
            json=payload,
            name="[heart] /generate-tests",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(4)
    def health(self):
        self.client.get("/health", name="[heart] /health")


class EarsTaskSet(TaskSet):
    """Ears engine (port 8002) — transcription."""

    @task(3)
    def list_jobs(self):
        self.client.get("/api/v1/ears/jobs", name="[ears] /jobs")

    @task(5)
    def health(self):
        self.client.get("/health", name="[ears] /health")


class EyesTaskSet(TaskSet):
    """Eyes engine (port 8003) — visual analysis."""

    @task(3)
    def list_jobs(self):
        self.client.get("/api/v1/eyes/jobs", name="[eyes] /jobs")

    @task(5)
    def health(self):
        self.client.get("/health", name="[eyes] /health")


class BackboneTaskSet(TaskSet):
    """Backbone engine (port 8005) — knowledge graph."""

    @task(3)
    def search_knowledge(self):
        payload = {
            "query": "premium calculation age band",
            "tenant_id": "t-1",
            "limit": 10,
        }
        with self.client.post(
            "/api/v1/backbone/search",
            json=payload,
            name="[backbone] /search",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(2)
    def get_stats(self):
        self.client.get("/api/v1/backbone/stats?tenant_id=t-1", name="[backbone] /stats")

    @task(4)
    def health(self):
        self.client.get("/health", name="[backbone] /health")


class LegsTaskSet(TaskSet):
    """Legs engine (port 8007) — test execution."""

    @task(3)
    def list_jobs(self):
        self.client.get("/api/v1/legs/jobs", name="[legs] /jobs")

    @task(5)
    def health(self):
        self.client.get("/health", name="[legs] /health")


class SpineTaskSet(TaskSet):
    """Spine engine (port 8009) — document processing."""

    @task(3)
    def list_documents(self):
        self.client.get("/api/v1/spine/documents?tenant_id=t-1", name="[spine] /documents")

    @task(5)
    def health(self):
        self.client.get("/health", name="[spine] /health")


class MouthTaskSet(TaskSet):
    """Mouth engine (port 8010) — report generation."""

    @task(3)
    def list_reports(self):
        self.client.get("/api/v1/mouth/reports?tenant_id=t-1", name="[mouth] /reports")

    @task(5)
    def health(self):
        self.client.get("/health", name="[mouth] /health")


class NervesTaskSet(TaskSet):
    """Nerves engine (port 8006) — external integrations."""

    @task(3)
    def list_connectors(self):
        self.client.get("/api/v1/nerves/connectors", name="[nerves] /connectors")

    @task(5)
    def health(self):
        self.client.get("/health", name="[nerves] /health")


class HandsTaskSet(TaskSet):
    """Hands engine (port 8008) — data generation."""

    @task(3)
    def generate_data(self):
        payload = {
            "schema": {
                "fields": [
                    {"name": "first_name", "type": "string"},
                    {"name": "age", "type": "integer", "min": 18, "max": 90},
                    {"name": "policy_number", "type": "string"},
                ]
            },
            "count": 5,
            "tenant_id": "t-1",
        }
        with self.client.post(
            "/api/v1/hands/generate",
            json=payload,
            name="[hands] /generate",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 422):
                resp.success()

    @task(5)
    def health(self):
        self.client.get("/health", name="[hands] /health")


# ─── Engine User Profiles ─────────────────────────────────────

class ShieldUser(HttpUser):
    """
    Load test Shield engine. Use --host http://localhost:8001
    Usage: locust -f tests/load/locustfile.py ShieldUser --host http://localhost:8001
    """
    wait_time = between(0.3, 1.0)
    weight = 0
    tasks = {ShieldTaskSet: 1}


class HeartUser(HttpUser):
    """
    Load test Heart engine. Use --host http://localhost:8004
    Usage: locust -f tests/load/locustfile.py HeartUser --host http://localhost:8004
    """
    wait_time = between(0.5, 2.0)
    weight = 0
    tasks = {HeartTaskSet: 1}


class EngineClusterUser(HttpUser):
    """
    Comprehensive engine load test via Gateway (port 8080).
    Tests all engines through the API gateway proxy.

    Usage: locust -f tests/load/locustfile.py EngineClusterUser --host http://localhost:8080
    """
    wait_time = between(0.5, 2.0)
    weight = 0

    tasks = {
        ShieldTaskSet: 3,
        HeartTaskSet: 2,
        EarsTaskSet: 1,
        EyesTaskSet: 1,
        BackboneTaskSet: 2,
        LegsTaskSet: 1,
        SpineTaskSet: 2,
        MouthTaskSet: 1,
        NervesTaskSet: 1,
        HandsTaskSet: 1,
    }


# ─── Custom event listeners ───────────────────────────────────

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("=" * 60)
    print("  NEXUS QA PLATFORM — LOAD TEST STARTING")
    print("=" * 60)
    print(f"  Target: {environment.host}")
    print(f"  Users:  {environment.parsed_options.num_users if environment.parsed_options else 'N/A'}")
    print("=" * 60)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    print("\n" + "=" * 60)
    print("  NEXUS QA PLATFORM — LOAD TEST COMPLETE")
    print("=" * 60)

    stats = environment.stats
    total = stats.total
    if total.num_requests > 0:
        print(f"  Total Requests:  {total.num_requests}")
        print(f"  Failures:        {total.num_failures} ({total.fail_ratio * 100:.1f}%)")
        print(f"  Avg Response:    {total.avg_response_time:.0f}ms")
        print(f"  95th Percentile: {total.get_response_time_percentile(0.95):.0f}ms")
        print(f"  99th Percentile: {total.get_response_time_percentile(0.99):.0f}ms")
        print(f"  Throughput:      {total.total_rps:.1f} req/s")

        # Pass/fail criteria
        p95 = total.get_response_time_percentile(0.95)
        fail_pct = total.fail_ratio * 100
        print()
        print(f"  {'✅' if p95 < 500 else '❌'} P95 < 500ms:     {p95:.0f}ms")
        print(f"  {'✅' if fail_pct < 1 else '❌'} Error rate < 1%: {fail_pct:.1f}%")
        print(f"  {'✅' if total.total_rps > 200 else '⚠️'} Throughput > 200: {total.total_rps:.1f} req/s")
    print("=" * 60)
