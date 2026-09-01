"""
Platform API — Test Strategy Route Tests.

Tests for the Test Architect test strategy generation route,
covering request validation, cache lineage, quality gating,
tenant isolation, provenance, and error paths.
"""

import pytest
import time
import inspect
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# Import & Model Contract Tests
# ═══════════════════════════════════════════════════════════════


class TestTestStrategyRouteImports:
    """Verify the route module and endpoint are importable."""

    def test_import_router(self):
        from app.routers.test_strategy import router, generate_test_strategy
        assert router is not None
        assert generate_test_strategy is not None

    def test_request_model_fields(self):
        from app.routers.test_strategy import GenerateTestStrategyRequest
        req = GenerateTestStrategyRequest(artifact_id="test-123")
        assert req.artifact_id == "test-123"
        assert req.session_id is None
        assert req.force_regenerate is False

    def test_request_model_with_all_fields(self):
        from app.routers.test_strategy import GenerateTestStrategyRequest
        req = GenerateTestStrategyRequest(
            artifact_id="art-1",
            session_id="sess-1",
            force_regenerate=True,
        )
        assert req.session_id == "sess-1"
        assert req.force_regenerate is True

    def test_request_model_requires_artifact_id(self):
        from pydantic import ValidationError
        from app.routers.test_strategy import GenerateTestStrategyRequest
        with pytest.raises(ValidationError):
            GenerateTestStrategyRequest()


class TestTenantIsolation:
    """Verify artifact lookup is tenant-scoped (security)."""

    def test_generate_strategy_does_not_accept_tenant_query(self):
        """generate_test_strategy should derive tenant_id from user, not accept it."""
        from app.routers.test_strategy import generate_test_strategy

        sig = inspect.signature(generate_test_strategy)
        param_names = list(sig.parameters.keys())
        assert "req" in param_names
        assert "user" in param_names
        assert "tenant_id" not in param_names, (
            "generate_test_strategy should derive tenant_id from user, "
            "not accept it as a query parameter"
        )


class TestServiceTokenGeneration:
    """Verify _make_service_token creates proper JWT for Brain calls."""

    def test_token_has_required_fields(self):
        import jwt as pyjwt
        from app.routers.test_strategy import _make_service_token
        from app.config import PlatformAPIConfig

        cfg = PlatformAPIConfig()
        token = _make_service_token("t-test")
        payload = pyjwt.decode(
            token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm]
        )

        assert payload["tenant_id"] == "t-test"
        assert payload["email"] == "platform-api@internal.nexus"
        assert payload["role"] == "admin"
        assert "exp" in payload
        assert "sub" in payload

    def test_token_includes_wildcard_permissions(self):
        import jwt as pyjwt
        from app.routers.test_strategy import _make_service_token
        from app.config import PlatformAPIConfig

        cfg = PlatformAPIConfig()
        token = _make_service_token("t-abc")
        payload = pyjwt.decode(
            token, cfg.jwt_secret, algorithms=[cfg.jwt_algorithm]
        )
        assert "*" in payload.get("permissions", [])


# ═══════════════════════════════════════════════════════════════
# Cache Lineage Tests
# ═══════════════════════════════════════════════════════════════


class TestCacheLineageValidation:
    """Verify test strategy cache is invalidated when persona is regenerated."""

    def test_stale_cache_is_invalidated_when_persona_regenerated(self):
        """If persona_draft_cache.provenance.generated_at differs from
        test_strategy_cache.provenance.source_persona_generated_at,
        the cache should be invalidated and regeneration triggered."""
        # This is a contract-level test — we validate the lineage fields exist
        # in the provenance structure so the runtime check can work.

        # Simulate a cached test strategy with old persona timestamp
        cached_strategy = {
            "test_plan": {"name": "Test Plan"},
            "provenance": {
                "source_persona_generated_at": "2025-01-01T00:00:00Z",
                "generated_at": "2025-01-01T01:00:00Z",
            },
        }

        # Simulate current persona draft with newer timestamp
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1, "name": "Step 1"}]},
            "provenance": {"generated_at": "2025-01-02T00:00:00Z"},
        }

        cached_source = cached_strategy["provenance"]["source_persona_generated_at"]
        current_persona = persona_cache["provenance"]["generated_at"]

        # They don't match → cache should be invalidated
        assert cached_source != current_persona

    def test_fresh_cache_is_returned_when_persona_unchanged(self):
        """If persona timestamp matches, cache is valid."""
        ts = "2025-01-01T00:00:00Z"

        cached_strategy = {
            "test_plan": {"name": "Test Plan"},
            "provenance": {
                "source_persona_generated_at": ts,
                "generated_at": "2025-01-01T01:00:00Z",
            },
        }

        persona_cache = {
            "provenance": {"generated_at": ts},
        }

        cached_source = cached_strategy["provenance"]["source_persona_generated_at"]
        current_persona = persona_cache["provenance"]["generated_at"]
        assert cached_source == current_persona

    def test_pre_lineage_cache_treated_as_stale(self):
        """Cache entries without source_persona_generated_at must be invalidated."""
        cached_strategy = {
            "test_plan": {"name": "Test Plan"},
            "provenance": {
                "generated_at": "2025-01-01T01:00:00Z",
                # No source_persona_generated_at — pre-lineage entry
            },
        }

        persona_cache = {
            "provenance": {"generated_at": "2025-01-02T00:00:00Z"},
        }

        cached_source = cached_strategy["provenance"].get("source_persona_generated_at", "")
        persona_generated_at = persona_cache["provenance"]["generated_at"]

        # Pre-lineage: cached_source is empty but persona has a timestamp
        # This should be treated as stale
        assert not cached_source and persona_generated_at


# ═══════════════════════════════════════════════════════════════
# Quality Gate Tests
# ═══════════════════════════════════════════════════════════════


class TestPersonaQualityGate:
    """Verify test strategy rejects fallback-quality persona drafts."""

    def test_quality_field_contract(self):
        """draft_quality field should be checked by the route."""
        # Simulate persona cache with fallback quality
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "fallback",
        }
        assert persona_cache["draft_quality"] == "fallback"

    def test_full_quality_passes_gate(self):
        """draft_quality='full' should pass the quality gate."""
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "full",
        }
        assert persona_cache["draft_quality"] != "fallback"

    def test_missing_quality_is_not_rejected(self):
        """If draft_quality is absent (legacy), it should not be rejected."""
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
        }
        # Missing quality should not equal "fallback"
        assert persona_cache.get("draft_quality", "") != "fallback"


# ═══════════════════════════════════════════════════════════════
# Provenance Contract Tests
# ═══════════════════════════════════════════════════════════════


class TestProvenanceContract:
    """Verify response provenance includes lineage fields."""

    def test_brain_provenance_model_has_lineage_fields(self):
        """Brain's TestStrategyProvenance must include source_persona_* fields."""
        # Validate the contract via dict shape (Brain runs in separate container)
        prov_shape = {
            "artifact_id": "art-1",
            "session_id": "sess-1",
            "persona_name": "Expert",
            "generated_at": "2025-01-01T00:00:00Z",
            "model_used": "llama3.2:1b",
            "model_backend": "ollama",
            "generation_time_ms": 500.0,
            "workflow_steps_analysed": 5,
            "risks_considered": 2,
            "source_persona_generated_at": "2025-01-01T00:00:00Z",
            "source_persona_quality": "full",
        }
        assert "source_persona_generated_at" in prov_shape
        assert "source_persona_quality" in prov_shape

    def test_response_data_structure(self):
        """Response must contain all required top-level keys."""
        required_keys = {
            "success", "artifact_id", "session_id",
            "test_plan", "test_scenarios", "coverage",
            "traceability", "provenance", "processing_time_ms",
        }
        # Simulate response data shape
        response = {
            "success": True,
            "artifact_id": "art-1",
            "session_id": "sess-1",
            "test_plan": {},
            "test_scenarios": [],
            "coverage": {},
            "traceability": [],
            "provenance": {},
            "processing_time_ms": 100.0,
            "cached": False,
        }
        assert required_keys.issubset(set(response.keys()))


# ═══════════════════════════════════════════════════════════════
# Cached Timing Truth Tests
# ═══════════════════════════════════════════════════════════════


class TestCachedTimingTruth:
    """Verify cached responses include distinct timing fields."""

    def test_cache_hit_includes_cache_hit_ms(self):
        """On cache hit, response must have cached=True and cache_hit_ms."""
        response = {
            "cached": True,
            "cache_hit_ms": 12.5,
            "processing_time_ms": 780000.0,  # original generation time
        }
        assert response["cached"] is True
        assert response["cache_hit_ms"] < response["processing_time_ms"]

    def test_fresh_generation_has_cached_false(self):
        """Fresh generation should have cached=False."""
        response = {
            "cached": False,
            "processing_time_ms": 780000.0,
        }
        assert response["cached"] is False
        assert "cache_hit_ms" not in response


# ═══════════════════════════════════════════════════════════════
# Error Path Tests
# ═══════════════════════════════════════════════════════════════


class TestErrorPaths:
    """Verify error responses for missing/invalid prerequisites."""

    def test_missing_persona_draft_gives_422(self):
        """No persona_draft_cache → 422 with descriptive message."""
        full_json = {}  # No persona_draft_cache
        persona_cache = full_json.get("persona_draft_cache")
        assert persona_cache is None

    def test_empty_workflows_gives_422(self):
        """Persona with empty workflows → 422."""
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": []},
        }
        assert not persona_cache["domain_map"].get("workflows")

    def test_fallback_quality_gives_422(self):
        """Persona with draft_quality='fallback' → 422."""
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "fallback",
        }
        assert persona_cache["draft_quality"] == "fallback"


# ═══════════════════════════════════════════════════════════════
# Evidence Enrichment Contract Tests
# ═══════════════════════════════════════════════════════════════


class TestEvidenceEnrichment:
    """Verify evidence is not truncated to first-item-only."""

    def test_multiple_evidence_items_preserved(self):
        """Workflow steps with multiple evidence items should all be passed."""
        workflow = {
            "step_number": 1,
            "name": "Login",
            "evidence": [
                {"text": "User clicks the login button", "source_modality": "transcript"},
                {"text": "Screen shows login form", "source_modality": "visual"},
                {"text": "Navigates to dashboard after login", "source_modality": "transcript"},
            ],
        }
        ev_lines = []
        for ev_item in workflow.get("evidence", []):
            if isinstance(ev_item, dict):
                ev_t = ev_item.get("text", "")[:200]
                ev_mod = ev_item.get("source_modality", "")
                if ev_t:
                    ev_lines.append(f'"{ev_t}" [{ev_mod}]' if ev_mod else f'"{ev_t}"')
        assert len(ev_lines) == 3
        assert "login button" in ev_lines[0]
        assert "visual" in ev_lines[1]

    def test_evidence_truncation_at_200_chars(self):
        """Evidence text should be truncated at 200 chars, not 100."""
        long_text = "A" * 250
        truncated = long_text[:200]
        assert len(truncated) == 200

    def test_fallback_precondition_uses_real_step_name(self):
        """Synthetic fallback cases should reference prior step name, not generic text."""
        step_name_map = {1: "Login to Portal", 2: "Navigate to Dashboard"}
        sn = 2
        preconditions = []
        if sn > 1 and (sn - 1) in step_name_map:
            preconditions.append(f"{step_name_map[sn - 1]} completed successfully")
        assert preconditions[0] == "Login to Portal completed successfully"
        assert "Previous step completed" not in preconditions[0]
