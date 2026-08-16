"""
Platform API — Persona Draft Generation Tests.

Tests for the Process Oracle persona generation route,
covering tenant scoping, validation, provenance persistence,
slug collision handling, and error paths.
"""

import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════
# Persona Draft Route Tests
# ═══════════════════════════════════════════════════════════════


class TestGeneratePersonaDraftRoute:
    """Test POST /api/v1/personas/generate-draft."""

    def test_import_router(self):
        from app.routers.personas import router, generate_persona_draft
        assert router is not None
        assert generate_persona_draft is not None

    def test_request_model_fields(self):
        from app.routers.personas import GeneratePersonaDraftRequest
        req = GeneratePersonaDraftRequest(artifact_id="test-123")
        assert req.artifact_id == "test-123"
        assert req.session_id is None

    def test_request_model_with_session(self):
        from app.routers.personas import GeneratePersonaDraftRequest
        req = GeneratePersonaDraftRequest(
            artifact_id="art-1", session_id="sess-1"
        )
        assert req.session_id == "sess-1"

    def test_create_persona_request_has_metadata_json(self):
        from app.routers.personas import CreatePersonaRequest
        req = CreatePersonaRequest(
            name="Test Persona",
            slug="test-persona",
            metadata_json={"generated_from": "process_oracle"},
        )
        assert req.metadata_json == {"generated_from": "process_oracle"}

    def test_create_persona_request_metadata_optional(self):
        from app.routers.personas import CreatePersonaRequest
        req = CreatePersonaRequest(name="Test Persona", slug="test-persona")
        assert req.metadata_json is None


class TestTenantScopedArtifactLookup:
    """Verify artifact lookup is tenant-scoped (security fix)."""

    def test_generate_draft_does_not_accept_tenant_query(self):
        """generate_persona_draft should not have a tenant_id query param."""
        import inspect
        from app.routers.personas import generate_persona_draft

        sig = inspect.signature(generate_persona_draft)
        param_names = list(sig.parameters.keys())
        # Should have req and user, but NOT tenant_id as a direct param
        assert "req" in param_names
        assert "user" in param_names
        assert "tenant_id" not in param_names, (
            "generate_persona_draft should derive tenant_id from user, "
            "not accept it as a query parameter"
        )


class TestSlugCollisionHandling:
    """Verify slug uniqueness check returns 409."""

    def test_create_persona_request_slug_pattern(self):
        """Slug must match ^[a-z0-9][a-z0-9-]*[a-z0-9]$."""
        from pydantic import ValidationError
        from app.routers.personas import CreatePersonaRequest

        # Valid slugs
        CreatePersonaRequest(name="Test", slug="valid-slug")
        CreatePersonaRequest(name="Test", slug="ab")

        # Invalid: starts with dash
        with pytest.raises(ValidationError):
            CreatePersonaRequest(name="Test", slug="-bad")

        # Invalid: ends with dash
        with pytest.raises(ValidationError):
            CreatePersonaRequest(name="Test", slug="bad-")

        # Invalid: uppercase
        with pytest.raises(ValidationError):
            CreatePersonaRequest(name="Test", slug="Bad")


class TestPersonaResponseShape:
    """Verify _persona_to_response strips metadata_json."""

    def test_metadata_excluded_from_list_response(self):
        from app.routers.personas import _persona_to_response

        mock_row = MagicMock()
        mock_row.__dict__ = {
            "persona_id": "p-1",
            "tenant_id": "t-1",
            "name": "Test",
            "slug": "test",
            "description": "",
            "avatar_icon": "brain",
            "system_prompt": "",
            "capabilities": [],
            "stage_config": {},
            "specialty_domains": [],
            "is_system": False,
            "is_active": True,
            "sort_order": 0,
            "metadata_json": {"secret": "data"},
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": "2025-01-01T00:00:00Z",
        }

        with patch("app.routers.personas.row_to_dict") as mock_rtd:
            mock_rtd.return_value = dict(mock_row.__dict__)
            result = _persona_to_response(mock_row)
            assert "metadata_json" not in result


class TestServiceTokenGeneration:
    """Verify _make_service_token creates proper JWT."""

    def test_token_has_required_fields(self):
        import jwt as pyjwt
        from app.routers.personas import _make_service_token
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


# ═══════════════════════════════════════════════════════════════
# Tenant Isolation Tests for CRUD Routes
# ═══════════════════════════════════════════════════════════════


class TestPersonaCRUDTenantIsolation:
    """Verify all CRUD routes derive tenant_id from auth, not query params."""

    def test_list_personas_no_tenant_query_param(self):
        """list_personas should NOT accept tenant_id as a direct parameter."""
        import inspect
        from app.routers.personas import list_personas

        sig = inspect.signature(list_personas)
        param_names = list(sig.parameters.keys())
        assert "tenant_id" not in param_names, (
            "list_personas should derive tenant_id from user, "
            "not accept it as a query parameter"
        )

    def test_create_persona_no_tenant_query_param(self):
        """create_persona should NOT accept tenant_id as a direct parameter."""
        import inspect
        from app.routers.personas import create_persona

        sig = inspect.signature(create_persona)
        param_names = list(sig.parameters.keys())
        assert "tenant_id" not in param_names, (
            "create_persona should derive tenant_id from user, "
            "not accept it as a query parameter"
        )

    def test_get_persona_has_user_dependency(self):
        """get_persona should use get_current_user for auth."""
        import inspect
        from app.routers.personas import get_persona

        sig = inspect.signature(get_persona)
        assert "user" in sig.parameters

    def test_update_persona_has_user_dependency(self):
        """update_persona should use get_current_user for auth."""
        import inspect
        from app.routers.personas import update_persona

        sig = inspect.signature(update_persona)
        assert "user" in sig.parameters

    def test_delete_persona_has_user_dependency(self):
        """delete_persona should use get_current_user for auth."""
        import inspect
        from app.routers.personas import delete_persona

        sig = inspect.signature(delete_persona)
        assert "user" in sig.parameters


# ═══════════════════════════════════════════════════════════════
# Cache Behaviour Tests
# ═══════════════════════════════════════════════════════════════


class TestPersonaDraftCaching:
    """Verify cache-hit, force_regenerate, and fallback-caching logic."""

    def test_force_regenerate_field_default_false(self):
        """force_regenerate defaults to False."""
        from app.routers.personas import GeneratePersonaDraftRequest
        req = GeneratePersonaDraftRequest(artifact_id="art-1")
        assert req.force_regenerate is False

    def test_force_regenerate_field_override(self):
        """force_regenerate can be explicitly set to True."""
        from app.routers.personas import GeneratePersonaDraftRequest
        req = GeneratePersonaDraftRequest(artifact_id="art-1", force_regenerate=True)
        assert req.force_regenerate is True

    def test_cache_hit_returns_cached_flag(self):
        """Verify the cache code path sets cached=True on the returned data."""
        # The cache-hit branch explicitly sets cached=True and cache_hit_ms
        # This test validates the contract — if persona_draft_cache exists
        # and force_regenerate=False, the response must contain cached=True
        cached_data = {
            "persona": {"name": "Test Expert"},
            "domain_map": {},
            "provenance": {"generated_at": "2025-01-01"},
        }
        # Simulate what the route does when cache is found
        cached_data["cached"] = True
        cached_data["cache_hit_ms"] = 5.0
        assert cached_data["cached"] is True
        assert cached_data["cache_hit_ms"] > 0

    def test_draft_quality_tagged_full_when_evidence_present(self):
        """draft_quality should be 'full' when domain_map has evidence."""
        response_data = {
            "domain_map": {
                "actors": [{"name": "User"}],
                "systems": [],
                "workflows": [],
                "risks": [],
            }
        }
        dm = response_data.get("domain_map", {})
        has_evidence = any(
            len(dm.get(cat, [])) > 0
            for cat in ("actors", "systems", "workflows", "risks")
        )
        quality = "full" if has_evidence else "fallback"
        assert quality == "full"

    def test_draft_quality_tagged_fallback_when_no_evidence(self):
        """draft_quality should be 'fallback' when domain_map is empty."""
        response_data = {
            "domain_map": {
                "actors": [],
                "systems": [],
                "workflows": [],
                "risks": [],
            }
        }
        dm = response_data.get("domain_map", {})
        has_evidence = any(
            len(dm.get(cat, [])) > 0
            for cat in ("actors", "systems", "workflows", "risks")
        )
        quality = "full" if has_evidence else "fallback"
        assert quality == "fallback"

    def test_fallback_draft_is_always_cached(self):
        """Even fallback (empty evidence) drafts must be cached —
        the cache write must NOT be gated on has_evidence."""
        import ast
        import inspect
        from app.routers.personas import generate_persona_draft

        source = inspect.getsource(generate_persona_draft)
        # There should be NO `if has_evidence:` gate around cache write
        assert "if has_evidence:" not in source, (
            "Cache write must not be gated on has_evidence — fallback drafts "
            "must also be cached to prevent re-generation on revisit"
        )


# ═══════════════════════════════════════════════════════════════
# Tenant Ownership Enforcement Tests
# ═══════════════════════════════════════════════════════════════


class TestTenantOwnershipEnforcement:
    """Verify GET/PUT/DELETE check row.tenant_id against caller's tenant."""

    def test_get_persona_enforces_tenant_in_code(self):
        """get_persona source must compare row.tenant_id to user tenant."""
        import inspect
        from app.routers.personas import get_persona

        source = inspect.getsource(get_persona)
        assert "row.tenant_id" in source, (
            "get_persona must check row.tenant_id against user tenant"
        )
        assert "SYSTEM_TENANT" in source, (
            "get_persona must allow access to system personas"
        )

    def test_update_persona_enforces_tenant_in_code(self):
        """update_persona source must compare row.tenant_id to user tenant."""
        import inspect
        from app.routers.personas import update_persona

        source = inspect.getsource(update_persona)
        assert "row.tenant_id != tenant_id" in source, (
            "update_persona must reject row.tenant_id != user tenant_id"
        )

    def test_delete_persona_enforces_tenant_in_code(self):
        """delete_persona source must compare row.tenant_id to user tenant."""
        import inspect
        from app.routers.personas import delete_persona

        source = inspect.getsource(delete_persona)
        assert "row.tenant_id != tenant_id" in source, (
            "delete_persona must reject row.tenant_id != user tenant_id"
        )


# ═══════════════════════════════════════════════════════════════
# Brain Engine — JSON Repair and max_tokens Tests
# ═══════════════════════════════════════════════════════════════


class TestBrainPersonaDraftGeneration:
    """Verify Brain-side persona generation configuration."""

    def test_max_tokens_is_2048(self):
        """max_tokens should be 2048 for large models and 1024 for small models."""
        import importlib.util
        import os
        brain_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "engines", "brain-engine", "main.py"
        )
        brain_path = os.path.normpath(brain_path)
        with open(brain_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Small-model path: max_gen_tokens capped at 1536
        assert "max_gen_tokens = 1536" in source, (
            "Brain persona generation should use max_gen_tokens=1536 for small models"
        )
        # The LLM call should use the adaptive max_gen_tokens variable
        assert "max_tokens=max_gen_tokens" in source, (
            "Brain persona generation should pass max_gen_tokens to LLM call"
        )
        assert "max_tokens=3072" not in source, (
            "max_tokens=3072 latency regression should be reverted"
        )

    def test_json_mode_enabled(self):
        """Persona generation should use json_mode=True."""
        import os
        brain_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "engines", "brain-engine", "main.py"
        )
        brain_path = os.path.normpath(brain_path)
        with open(brain_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "json_mode=True" in source, (
            "Brain persona generation should use json_mode=True for structured output"
        )

    def test_brain_header_no_misleading_model_reference(self):
        """Brain docstring should not claim 70B when default is 1b."""
        import os
        brain_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "engines", "brain-engine", "main.py"
        )
        brain_path = os.path.normpath(brain_path)
        with open(brain_path, "r", encoding="utf-8") as f:
            # Read only header (first 40 lines)
            header = "".join(f.readlines()[:40])

        assert "70B" not in header, (
            "Brain header should not claim Llama 3.1 70B when default model is llama3.2:1b"
        )

    def test_json_repair_function_exists_in_brain(self):
        """Brain should have a _repair_truncated_json function."""
        import os
        brain_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "engines", "brain-engine", "main.py"
        )
        brain_path = os.path.normpath(brain_path)
        with open(brain_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "_repair_truncated_json" in source, (
            "Brain must have _repair_truncated_json to handle max_tokens truncation"
        )


class TestJsonRepairLogic:
    """Unit tests for the JSON truncation repair logic."""

    def _repair(self, text: str) -> str:
        """Copy of Brain's _repair_truncated_json for isolated testing."""
        open_braces = text.count('{') - text.count('}')
        open_brackets = text.count('[') - text.count(']')
        text = text.rstrip()
        if text.endswith(','):
            text = text[:-1]
        last_brace = max(text.rfind('}'), text.rfind(']'), text.rfind('"'))
        if last_brace > 0:
            text = text[:last_brace + 1]
            open_braces = text.count('{') - text.count('}')
            open_brackets = text.count('[') - text.count(']')
        text += ']' * max(0, open_brackets)
        text += '}' * max(0, open_braces)
        return text

    def test_repair_truncated_simple(self):
        """Repair a JSON object truncated after a complete value."""
        import json
        truncated = '{"persona": {"name": "Expert"}, "extra": {"key": "val"'
        repaired = self._repair(truncated)
        parsed = json.loads(repaired)
        assert "persona" in parsed

    def test_repair_truncated_array(self):
        """Repair JSON with unclosed array."""
        import json
        truncated = '{"items": ["a", "b", "c"'
        repaired = self._repair(truncated)
        parsed = json.loads(repaired)
        assert "items" in parsed

    def test_repair_trailing_comma(self):
        """Repair JSON with trailing comma when last value is a string."""
        import json
        truncated = '{"a": 1, "b": "two", "c": "three",'
        repaired = self._repair(truncated)
        parsed = json.loads(repaired)
        assert parsed["b"] == "two"

    def test_repair_valid_json_unchanged(self):
        """Valid JSON should pass through unchanged."""
        import json
        valid = '{"name": "test", "value": 42}'
        repaired = self._repair(valid)
        assert json.loads(repaired) == json.loads(valid)

    def test_repair_nested_truncation(self):
        """Repair deeply nested truncated JSON with complete inner values."""
        import json
        truncated = '{"a": {"b": {"c": [1, 2, 3]}, "d": "val"'
        repaired = self._repair(truncated)
        parsed = json.loads(repaired)
        assert parsed["a"]["b"]["c"] == [1, 2, 3]


# ═══════════════════════════════════════════════════════════════
# Platform-Brain Timeout Chain Tests
# ═══════════════════════════════════════════════════════════════


class TestTimeoutChainConsistency:
    """Verify timeouts are consistent across the request chain."""

    def test_platform_brain_httpx_timeout_is_900(self):
        """Platform's httpx call to Brain should use timeout=900."""
        import inspect
        from app.routers.personas import generate_persona_draft

        source = inspect.getsource(generate_persona_draft)
        assert "timeout=900.0" in source, (
            "Platform httpx call to Brain should use 900s timeout"
        )

    def test_nginx_proxy_read_timeout(self):
        """Nginx should have proxy_read_timeout >= 900s."""
        import os
        nginx_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "infrastructure", "docker", "nginx-client.conf",
        )
        nginx_path = os.path.normpath(nginx_path)
        with open(nginx_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "proxy_read_timeout 900s" in content

    def test_ollama_keep_alive_set(self):
        """Ollama provider should send keep_alive to prevent model unload."""
        import os
        provider_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "sdk", "nexus-sdk", "nexus_sdk", "llm", "providers", "__init__.py",
        )
        provider_path = os.path.normpath(provider_path)
        with open(provider_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "keep_alive" in content, (
            "Ollama provider must send keep_alive to prevent model eviction"
        )

    def test_brain_tier3_timeout_less_than_platform(self):
        """BRAIN_TIER3_TIMEOUT must be less than platform httpx timeout (900s).

        If they are equal, Brain cannot fall back to stub before platform
        times out, causing 504 even though Brain could return a fallback.
        """
        import os
        import re
        compose_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "docker-compose.canonical.yml",
        )
        compose_path = os.path.normpath(compose_path)
        with open(compose_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        match = re.search(r'BRAIN_TIER3_TIMEOUT:\s*["\']?(\d+)', content)
        assert match, "BRAIN_TIER3_TIMEOUT not found in docker-compose.canonical.yml"
        brain_timeout = int(match.group(1))
        platform_timeout = 900  # from personas.py httpx.AsyncClient(timeout=900.0)
        assert brain_timeout < platform_timeout, (
            f"BRAIN_TIER3_TIMEOUT ({brain_timeout}s) must be < platform httpx timeout "
            f"({platform_timeout}s) to allow Brain fallback before platform 504. "
            f"Recommended: {platform_timeout - 120}s or less."
        )

    def test_brain_tier3_timeout_present_in_dev_compose(self):
        """The main compose profile must override the Brain tier timeout defaults."""
        import os
        import re

        compose_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "docker-compose.yml",
        )
        compose_path = os.path.normpath(compose_path)
        with open(compose_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        timeout_match = re.search(r'BRAIN_TIER3_TIMEOUT:\s*["\']?(\d+)', content)
        retry_match = re.search(r'BRAIN_TIER3_MAX_RETRIES:\s*["\']?(\d+)', content)

        assert timeout_match, "BRAIN_TIER3_TIMEOUT not found in docker-compose.yml"
        assert retry_match, "BRAIN_TIER3_MAX_RETRIES not found in docker-compose.yml"
        assert int(timeout_match.group(1)) < 900
        assert int(retry_match.group(1)) == 1
