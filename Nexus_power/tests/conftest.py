"""
Nexus QA — Root test configuration.

Provides shared fixtures for all test modules:
  - JWT tokens and auth helpers
  - Async test support
  - Common test data factories
  - Path setup for imports
"""

import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

# ─── Path Setup ────────────────────────────────────────────────
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Add engine paths so `from main import ...` works in each test
ENGINE_DIRS = [
    os.path.join(PROJECT_ROOT, "engines", d)
    for d in [
        "shield-engine", "ears-engine", "eyes-engine", "heart-engine",
        "backbone-engine", "nerves-engine", "legs-engine", "hands-engine",
        "spine-engine", "mouth-engine",
    ]
]
SDK_PATH = os.path.join(PROJECT_ROOT, "sdk", "nexus-sdk")
ORCHESTRATOR_PATH = os.path.join(PROJECT_ROOT, "products", "nexus-qa-orchestrator")

for p in ENGINE_DIRS + [SDK_PATH, ORCHESTRATOR_PATH]:
    if p not in sys.path:
        sys.path.insert(0, p)


# ─── Auth Fixtures ─────────────────────────────────────────────

JWT_SECRET = "test-secret-do-not-use-in-production"


@pytest.fixture
def auth_service():
    """Initialized AuthService for testing."""
    from nexus_sdk.auth import AuthService
    return AuthService(jwt_secret=JWT_SECRET)


@pytest.fixture
def test_user():
    """Standard test user."""
    from nexus_sdk.auth import NexusUser
    return NexusUser(
        user_id="test-user-001",
        tenant_id="test-tenant-001",
        email="tester@nexus.local",
        role="admin",
        permissions=["*"],
        name="Test User",
    )


@pytest.fixture
def test_token(auth_service, test_user):
    """Valid JWT token for test_user."""
    return auth_service.create_token(test_user)


@pytest.fixture
def auth_headers(test_token):
    """Authorization headers dict for HTTP requests."""
    return {"Authorization": f"Bearer {test_token}"}


@pytest.fixture
def viewer_user():
    """Low-privilege viewer user."""
    from nexus_sdk.auth import NexusUser
    return NexusUser(
        user_id="viewer-001",
        tenant_id="test-tenant-001",
        email="viewer@nexus.local",
        role="viewer",
        permissions=["read"],
        name="Viewer",
    )


@pytest.fixture
def other_tenant_user():
    """User from a different tenant."""
    from nexus_sdk.auth import NexusUser
    return NexusUser(
        user_id="other-user",
        tenant_id="other-tenant-999",
        email="other@nexus.local",
        role="admin",
        permissions=["*"],
    )


# ─── Common Test Data ─────────────────────────────────────────

@pytest.fixture
def sample_pii_text():
    return (
        "Mr. John Smith, SSN 123-45-6789, email: john@example.com, "
        "phone: (555) 867-5309, policy PLY-2024-AB-99887766"
    )


@pytest.fixture
def sample_transcript():
    return (
        "So for the life insurance product, we have a minimum issue age of 18 "
        "and maximum of 75. The premium rate for standard non-tobacco is 4.25 "
        "per thousand for ages 35 to 40. If the insured is a smoker, we apply "
        "a 75% surcharge. The waiver of premium rider is available for an "
        "additional 15% of base premium."
    )


@pytest.fixture
def sample_business_rule():
    from nexus_sdk.models import BusinessRule, SourceReference
    return BusinessRule(
        rule_id="rule-test-001",
        tenant_id="test-tenant-001",
        product="Universal Life",
        jurisdiction="NY",
        category="premium",
        rule_text="Standard non-tobacco rate is 4.25 per thousand for ages 35-40",
        conditions=["age >= 35", "age <= 40", "tobacco_status == non_tobacco"],
        source=SourceReference(
            session_id="session-001",
            confidence="high",
        ),
        tags=["rate", "premium", "age-band"],
    )


@pytest.fixture
def sample_test_case():
    from nexus_sdk.models import TestCase, TestStep
    return TestCase(
        test_id="tc-test-001",
        tenant_id="test-tenant-001",
        title="Verify standard non-tobacco premium rate",
        description="Check rate of 4.25/thousand for 35-40 non-tobacco",
        steps=[
            TestStep(
                step_number=1,
                action="Navigate to rating page",
                target_system="web",
                target_element="a[href='/rating']",
            ),
            TestStep(
                step_number=2,
                action="Enter age 37",
                target_system="web",
                target_element="input#age",
                input_data={"value": "37"},
            ),
            TestStep(
                step_number=3,
                action="Verify rate displayed",
                target_system="web",
                target_element="span.rate-value",
                expected_output="4.25",
            ),
        ],
        validates_rules=["rule-test-001"],
        priority="high",
        tags=["premium", "rate"],
    )


# ─── Utility Helpers ──────────────────────────────────────────

def make_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
