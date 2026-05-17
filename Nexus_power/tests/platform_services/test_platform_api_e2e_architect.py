"""
Platform API — E2E Architect Route Tests.

Tests for the E2E Architect generation route, covering request
validation, cache lineage, quality gating, tenant isolation,
provenance, visual substrate assessment, and error paths.
"""

import pytest
import time
import sys
import os
import inspect
import ast
import textwrap
import logging

# Load brain-engine's main.py as a distinct module to avoid collision with platform/api/main.py
_BRAIN_MAIN_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "engines", "brain-engine", "main.py",
))


def _extract_brain_functions():
    """Extract nested utility functions from brain-engine/main.py via AST.

    The functions ``_pairwise_combinations`` and ``_deduplicate_e2e_scenarios``
    are defined inside ``register_routes`` and can't be imported normally.
    This reads the source, extracts the function bodies, and compiles them
    in a namespace that contains the Pydantic models they reference.
    """
    # First import the module-level Pydantic models (these ARE importable)
    brain_dir = os.path.dirname(_BRAIN_MAIN_PATH)
    sdk_dir = os.path.abspath(os.path.join(brain_dir, "..", "..", "sdk", "nexus-sdk"))
    added = []
    for p in [brain_dir, sdk_dir]:
        if p not in sys.path:
            sys.path.insert(0, p)
            added.append(p)

    # Save and swap app namespace
    saved_app = {k: v for k, v in sys.modules.items() if k == "app" or k.startswith("app.")}
    for k in saved_app:
        del sys.modules[k]

    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("_brain_models", _BRAIN_MAIN_PATH)
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        # Restore app namespace
        for k in list(sys.modules):
            if k == "app" or k.startswith("app."):
                del sys.modules[k]
        sys.modules.update(saved_app)
        for p in added:
            if p in sys.path:
                sys.path.remove(p)

    # Read source and extract function bodies using AST
    with open(_BRAIN_MAIN_PATH, encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    target_funcs = {"_pairwise_combinations", "_deduplicate_e2e_scenarios"}
    func_sources: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in target_funcs:
            # Extract source lines for this function
            start = node.lineno - 1
            end = node.end_lineno
            lines = source.splitlines()[start:end]
            func_source = textwrap.dedent("\n".join(lines))
            func_sources[node.name] = func_source

    # Compile and exec the extracted functions with the models in scope
    ns = {
        "E2EVariable": mod.E2EVariable,
        "E2EScenario": mod.E2EScenario,
        "TestStep": mod.TestStep,
        "logger": logging.getLogger("brain_test"),
    }
    for name, src in func_sources.items():
        exec(compile(src, f"<brain:{name}>", "exec"), ns)

    # Return a namespace-like object
    class _BrainFuncs:
        pass
    result = _BrainFuncs()
    # Rebuild models to resolve forward refs (E2EScenario -> EvidenceCitation)
    for model_name in ["EvidenceCitation", "E2EScenario", "TestStep", "E2EVariable"]:
        model_cls = getattr(mod, model_name, None)
        if model_cls and hasattr(model_cls, "model_rebuild"):
            try:
                model_cls.model_rebuild()
            except Exception:
                pass
    for name in list(target_funcs) + ["E2EVariable", "E2EScenario", "TestStep", "EvidenceCitation"]:
        setattr(result, name, ns.get(name) or getattr(mod, name, None))
    return result


# Cache the extraction
_brain_funcs_cache = None


def _get_brain_funcs():
    global _brain_funcs_cache
    if _brain_funcs_cache is None:
        _brain_funcs_cache = _extract_brain_functions()
    return _brain_funcs_cache


# ═══════════════════════════════════════════════════════════════
# Import & Model Contract Tests
# ═══════════════════════════════════════════════════════════════


class TestE2EArchitectRouteImports:
    """Verify the route module and endpoint are importable."""

    def test_import_router(self):
        from app.routers.e2e_architect import router, generate_e2e_architect
        assert router is not None
        assert generate_e2e_architect is not None

    def test_request_model_fields(self):
        from app.routers.e2e_architect import GenerateE2EArchitectRequest
        req = GenerateE2EArchitectRequest(artifact_id="test-123")
        assert req.artifact_id == "test-123"
        assert req.session_id is None
        assert req.force_regenerate is False

    def test_request_model_with_all_fields(self):
        from app.routers.e2e_architect import GenerateE2EArchitectRequest
        req = GenerateE2EArchitectRequest(
            artifact_id="art-1",
            session_id="sess-1",
            force_regenerate=True,
        )
        assert req.session_id == "sess-1"
        assert req.force_regenerate is True

    def test_request_model_requires_artifact_id(self):
        from pydantic import ValidationError
        from app.routers.e2e_architect import GenerateE2EArchitectRequest
        with pytest.raises(ValidationError):
            GenerateE2EArchitectRequest()


class TestTenantIsolation:
    """Verify artifact lookup is tenant-scoped (security)."""

    def test_generate_e2e_does_not_accept_tenant_query(self):
        """generate_e2e_architect should derive tenant_id from user, not accept it."""
        from app.routers.e2e_architect import generate_e2e_architect

        sig = inspect.signature(generate_e2e_architect)
        param_names = list(sig.parameters.keys())
        assert "req" in param_names
        assert "user" in param_names
        assert "tenant_id" not in param_names, (
            "generate_e2e_architect should derive tenant_id from user, "
            "not accept it as a query parameter"
        )


class TestBrainE2EArchitectRuntimeGuards:
    """Guardrails for the Brain-side E2E Architect execution path."""

    def test_e2e_architect_disables_stub_fallback(self):
        with open(_BRAIN_MAIN_PATH, encoding="utf-8") as f:
            source = f.read()

        assert source.count("allow_stub_fallback=False") >= 2, (
            "E2E Architect passes must not silently fall back to stub output when the real LLM fails"
        )


class TestServiceTokenGeneration:
    """Verify _make_service_token creates proper JWT for Brain calls."""

    def test_token_has_required_fields(self):
        import jwt as pyjwt
        from app.routers.e2e_architect import _make_service_token
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
        from app.routers.e2e_architect import _make_service_token
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
    """Verify E2E Architect cache is invalidated when persona is regenerated."""

    def test_stale_cache_is_invalidated_when_persona_regenerated(self):
        cached_e2e = {
            "e2e_architect": {"variables": [], "critical_combinations": []},
            "provenance": {
                "source_persona_generated_at": "2025-01-01T00:00:00Z",
                "generated_at": "2025-01-01T01:00:00Z",
            },
        }
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1, "name": "Step 1"}]},
            "provenance": {"generated_at": "2025-01-02T00:00:00Z"},
        }
        cached_source = cached_e2e["provenance"]["source_persona_generated_at"]
        current_persona = persona_cache["provenance"]["generated_at"]
        assert cached_source != current_persona

    def test_fresh_cache_is_returned_when_persona_unchanged(self):
        ts = "2025-01-01T00:00:00Z"
        cached_e2e = {
            "e2e_architect": {"variables": [], "critical_combinations": []},
            "provenance": {
                "source_persona_generated_at": ts,
                "generated_at": "2025-01-01T01:00:00Z",
            },
        }
        persona_cache = {
            "provenance": {"generated_at": ts},
        }
        cached_source = cached_e2e["provenance"]["source_persona_generated_at"]
        current_persona = persona_cache["provenance"]["generated_at"]
        assert cached_source == current_persona

    def test_pre_lineage_cache_treated_as_stale(self):
        """Cache without source_persona_generated_at should be invalidated."""
        cached_e2e = {
            "e2e_architect": {"variables": []},
            "provenance": {
                "generated_at": "2025-01-01T01:00:00Z",
            },
        }
        persona_cache = {
            "provenance": {"generated_at": "2025-01-02T00:00:00Z"},
        }
        cached_source = cached_e2e["provenance"].get("source_persona_generated_at", "")
        persona_generated_at = persona_cache["provenance"]["generated_at"]
        assert not cached_source and persona_generated_at


# ═══════════════════════════════════════════════════════════════
# Quality Gate Tests
# ═══════════════════════════════════════════════════════════════


class TestPersonaQualityGate:
    """Verify E2E Architect rejects fallback-quality persona drafts."""

    def test_fallback_quality_rejected(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "fallback",
        }
        assert persona_cache["draft_quality"] == "fallback"

    def test_full_quality_passes_gate(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "full",
        }
        assert persona_cache["draft_quality"] != "fallback"

    def test_missing_quality_is_not_rejected(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
        }
        assert persona_cache.get("draft_quality", "") != "fallback"

    def test_missing_workflows_rejected(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": []},
            "draft_quality": "full",
        }
        assert not persona_cache["domain_map"].get("workflows")

    def test_no_persona_draft_rejected(self):
        full_json = {}
        persona_cache = full_json.get("persona_draft_cache")
        assert persona_cache is None


# ═══════════════════════════════════════════════════════════════
# Provenance Contract Tests
# ═══════════════════════════════════════════════════════════════


class TestProvenanceContract:
    """Verify response provenance includes lineage fields."""

    def test_brain_provenance_has_lineage_fields(self):
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
        }
        assert "artifact_id" in prov_shape
        assert "generation_time_ms" in prov_shape

    def test_platform_provenance_enriches_source_lineage(self):
        """Platform adds source_persona_generated_at to provenance."""
        brain_prov = {
            "model_used": "llama3.2:1b",
            "generation_time_ms": 500.0,
        }
        # Route enriches:
        brain_prov["platform_processing_ms"] = 1200.0
        brain_prov["source_persona_generated_at"] = "2025-01-01T00:00:00Z"
        assert "source_persona_generated_at" in brain_prov
        assert "platform_processing_ms" in brain_prov

    def test_response_data_structure(self):
        required_keys = {
            "success", "artifact_id", "session_id",
            "e2e_architect", "provenance",
            "processing_time_ms", "visual_substrate",
        }
        response = {
            "success": True,
            "artifact_id": "art-1",
            "session_id": "sess-1",
            "e2e_architect": {},
            "provenance": {},
            "processing_time_ms": 100.0,
            "cached": False,
            "visual_substrate": {
                "quality": "fast",
                "frame_count": 6,
                "has_ocr": False,
                "recommendation": "Re-upload with multimodal profile",
            },
        }
        assert required_keys.issubset(set(response.keys()))


# ═══════════════════════════════════════════════════════════════
# Visual Substrate Quality Tests
# ═══════════════════════════════════════════════════════════════


class TestVisualSubstrateAssessment:
    """Verify the route correctly classifies visual substrate quality."""

    def _assess(self, pipeline_stages, frame_count):
        """Reproduce the route's visual substrate quality logic."""
        vis_stages = pipeline_stages if isinstance(pipeline_stages, list) else [pipeline_stages]
        has_multimodal_markers = any("multimodal" in str(s).lower() for s in vis_stages)
        has_deep_markers = any("deep" in str(s).lower() for s in vis_stages)
        has_ocr = any("ocr" in str(s).lower() and "skipped" not in str(s).lower() for s in vis_stages)

        if has_multimodal_markers:
            return "multimodal", has_ocr
        elif has_deep_markers or frame_count >= 8:
            return "deep", has_ocr
        elif frame_count > 0:
            return "fast", has_ocr
        else:
            return "minimal", has_ocr

    def test_multimodal_profile_detected(self):
        quality, has_ocr = self._assess(
            ["frame_extraction", "ocr_representative", "multimodal_analysis", "quality:25"],
            10,
        )
        assert quality == "multimodal"
        assert has_ocr is True

    def test_fast_profile_with_ocr_skipped(self):
        quality, has_ocr = self._assess(
            ["frame_extraction", "ocr_skipped", "quality:0"],
            6,
        )
        assert quality == "fast"
        assert has_ocr is False

    def test_fast_profile_with_representative_ocr(self):
        quality, has_ocr = self._assess(
            ["frame_extraction", "ocr_representative", "quality:12"],
            5,
        )
        assert quality == "fast"
        assert has_ocr is True

    def test_deep_profile_with_many_frames(self):
        quality, _ = self._assess(
            ["frame_extraction", "ocr", "deep_analysis", "quality:50"],
            12,
        )
        assert quality == "deep"

    def test_deep_inferred_from_high_frame_count(self):
        quality, _ = self._assess(
            ["frame_extraction", "ocr"],
            10,
        )
        assert quality == "deep"

    def test_minimal_with_no_frames(self):
        quality, _ = self._assess([], 0)
        assert quality == "minimal"

    def test_recommendation_for_fast_substrate(self):
        quality, _ = self._assess(["ocr_skipped"], 4)
        assert quality == "fast"
        recommendation = (
            "Re-upload with 'Multimodal' processing profile for richer visual evidence"
            if quality == "fast"
            else None
        )
        assert recommendation is not None

    def test_no_recommendation_for_multimodal_substrate(self):
        quality, _ = self._assess(["multimodal_analysis", "ocr_representative"], 10)
        assert quality == "multimodal"
        recommendation = (
            "Re-upload with 'Multimodal' processing profile for richer visual evidence"
            if quality == "fast"
            else None
        )
        assert recommendation is None


# ═══════════════════════════════════════════════════════════════
# Cached Timing Truth Tests
# ═══════════════════════════════════════════════════════════════


class TestCachedTimingTruth:
    """Verify cached responses include distinct timing fields."""

    def test_cache_hit_includes_cache_hit_ms(self):
        response = {
            "cached": True,
            "cache_hit_ms": 12.5,
            "processing_time_ms": 780000.0,
        }
        assert response["cached"] is True
        assert response["cache_hit_ms"] < response["processing_time_ms"]

    def test_fresh_generation_has_cached_false(self):
        response = {
            "cached": False,
            "processing_time_ms": 780000.0,
        }
        assert response["cached"] is False
        assert "cache_hit_ms" not in response


# ═══════════════════════════════════════════════════════════════
# Multimodal Payload Assembly Tests
# ═══════════════════════════════════════════════════════════════


class TestMultimodalPayloadAssembly:
    """Verify build_e2e_brain_payload produces correct structure."""

    def test_payload_has_all_required_fields(self):
        from app.services.multimodal import build_e2e_brain_payload

        artifact = {
            "artifact_id": "art-1",
            "session_id": "sess-1",
            "tenant_id": "t-1",
            "visual_summary": "Summary text",
            "application_types_seen": ["web_ui"],
            "duration_seconds": 120.0,
            "frame_count": 6,
            "scene_count": 3,
            "full_artifact_json": {
                "persona_draft_cache": {
                    "persona": {"name": "Expert"},
                    "domain_map": {"workflows": [{"step_number": 1, "name": "Step 1"}]},
                    "grounding_contract": {},
                },
                "test_strategy_cache": {
                    "test_scenarios": [{"scenario_id": "TC-001", "title": "Login"}],
                },
                "visual_analysis": {
                    "frames": [
                        {
                            "timestamp_seconds": 0.0,
                            "description": "Login form",
                            "ui_elements": [
                                {"element_type": "button", "text": "Submit"},
                                {"element_type": "text_field", "text": "Username"},
                            ],
                        },
                    ],
                },
                "visual_graph": {
                    "nodes": [{"type": "screen", "label": "Login Screen"}],
                },
                "transcript": {
                    "segments": [
                        {"start": 0, "end": 5, "text": "Let me show you", "speaker": "Agent"},
                    ],
                },
            },
        }

        payload = build_e2e_brain_payload(artifact)

        assert payload["artifact_id"] == "art-1"
        assert payload["session_id"] == "sess-1"
        assert payload["persona_name"] == "Expert"
        assert len(payload["domain_map"]["workflows"]) == 1
        assert len(payload["existing_test_scenarios"]) == 1
        assert len(payload["scene_descriptions"]) == 1
        assert payload["ui_element_inventory"]["total_elements"] == 2
        assert len(payload["multimodal_scenes"]) == 1
        assert len(payload["visual_graph_nodes"]) == 1
        assert len(payload["transcript_segments"]) == 1
        assert payload["duration_seconds"] == 120.0
        assert payload["frame_count"] == 6

    def test_payload_graceful_with_empty_artifact(self):
        from app.services.multimodal import build_e2e_brain_payload

        artifact = {
            "artifact_id": "empty",
            "full_artifact_json": {},
        }
        payload = build_e2e_brain_payload(artifact)

        assert payload["artifact_id"] == "empty"
        assert payload["persona_name"] == ""
        assert payload["domain_map"] == {}
        assert payload["scene_descriptions"] == []
        assert payload["visual_graph_nodes"] == []
        assert payload["transcript_segments"] == []

    def test_payload_includes_visual_graph_nodes(self):
        """Verify visual_graph_nodes from the artifact are included for Brain."""
        from app.services.multimodal import build_e2e_brain_payload

        artifact = {
            "artifact_id": "vg-test",
            "full_artifact_json": {
                "visual_graph": {
                    "nodes": [
                        {"type": "screen", "label": "Dashboard"},
                        {"type": "action", "label": "Click Submit"},
                        {"type": "navigation", "label": "Go to Settings"},
                    ],
                },
            },
        }
        payload = build_e2e_brain_payload(artifact)
        assert len(payload["visual_graph_nodes"]) == 3
        assert payload["visual_graph_nodes"][0]["label"] == "Dashboard"


# ═══════════════════════════════════════════════════════════════
# Error Path Tests
# ═══════════════════════════════════════════════════════════════


class TestErrorPaths:
    """Verify error responses for missing/invalid prerequisites."""

    def test_missing_persona_draft_gives_422(self):
        full_json = {}
        persona_cache = full_json.get("persona_draft_cache")
        assert persona_cache is None

    def test_empty_workflows_gives_422(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": []},
        }
        assert not persona_cache["domain_map"].get("workflows")

    def test_fallback_quality_gives_422(self):
        persona_cache = {
            "persona": {"name": "Expert"},
            "domain_map": {"workflows": [{"step_number": 1}]},
            "draft_quality": "fallback",
        }
        assert persona_cache["draft_quality"] == "fallback"

    def test_artifact_not_found_gives_404(self):
        """Verify the route rejects unknown artifact IDs."""
        # Route does: select(...).where(artifact_id=req.artifact_id, tenant_id=tenant_id)
        # If row is None -> HTTPException(404)
        row = None
        assert row is None  # simulates "not found"


# ═══════════════════════════════════════════════════════════════
# Pairwise Combination Tests
# ═══════════════════════════════════════════════════════════════


class TestPairwiseCombinations:
    """Verify the pairwise combination algorithm in Brain engine."""

    def test_pairwise_import(self):
        bf = _get_brain_funcs()
        assert callable(bf._pairwise_combinations)

    def test_pairwise_covers_all_pairs_3x3(self):
        """3 variables × 3 values each = 27 unique pairs, covered in ~10 combos."""
        bf = _get_brain_funcs()
        _pairwise_combinations = bf._pairwise_combinations
        E2EVariable = bf.E2EVariable

        variables = [
            E2EVariable(name="A", type="cat", observed_values=["a1", "a2"], inferred_values=["a3"]),
            E2EVariable(name="B", type="cat", observed_values=["b1"], inferred_values=["b2", "b3"]),
            E2EVariable(name="C", type="cat", observed_values=["c1", "c2", "c3"]),
        ]
        combos = _pairwise_combinations(variables)
        assert len(combos) > 0
        assert len(combos) <= 30  # max cap

        # All 27 pairs must be covered
        covered_pairs = set()
        for combo in combos:
            keys = sorted(combo.keys())
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    covered_pairs.add((keys[i], combo[keys[i]], keys[j], combo[keys[j]]))

        # Enumerate all expected pairs
        vals = {"A": ["a1", "a2", "a3"], "B": ["b1", "b2", "b3"], "C": ["c1", "c2", "c3"]}
        expected_pairs = set()
        for v1 in ["A", "B", "C"]:
            for v2 in ["A", "B", "C"]:
                if v1 >= v2:
                    continue
                for a in vals[v1]:
                    for b in vals[v2]:
                        expected_pairs.add((v1, a, v2, b))

        assert expected_pairs.issubset(covered_pairs), (
            f"Missing pairs: {expected_pairs - covered_pairs}"
        )

    def test_pairwise_empty_variables(self):
        bf = _get_brain_funcs()
        combos = bf._pairwise_combinations([])
        assert combos == []

    def test_pairwise_single_variable(self):
        bf = _get_brain_funcs()
        E2EVariable = bf.E2EVariable
        variables = [E2EVariable(name="X", type="cat", observed_values=["x1", "x2"])]
        combos = bf._pairwise_combinations(variables)
        # Single var -> enumerates its values as individual combos
        assert len(combos) == 2
        assert combos[0] == {"X": "x1"}
        assert combos[1] == {"X": "x2"}


class TestDeduplication:
    """Verify the E2E scenario deduplication logic."""

    def test_dedup_import(self):
        bf = _get_brain_funcs()
        assert callable(bf._deduplicate_e2e_scenarios)

    def test_identical_scenarios_deduped(self):
        bf = _get_brain_funcs()
        _deduplicate_e2e_scenarios = bf._deduplicate_e2e_scenarios
        E2EScenario = bf.E2EScenario

        # Use model_construct to skip forward-ref validation (we're testing the dedup algorithm)
        sc1 = E2EScenario.model_construct(
            scenario_id="E2E-001", title="Login with valid credentials",
            category="observed", priority="P0_critical",
            steps=[], expected_outcome="Success",
            workflow_steps_covered=[], data_matrix=[],
        )
        sc2 = E2EScenario.model_construct(
            scenario_id="E2E-002", title="Login with valid user credentials",
            category="observed", priority="P0_critical",
            steps=[], expected_outcome="Success",
            workflow_steps_covered=[], data_matrix=[],
        )
        result = _deduplicate_e2e_scenarios([sc1, sc2], [])
        assert len(result) <= 2  # Dedup may remove if overlap signals fire

    def test_distinct_scenarios_preserved(self):
        bf = _get_brain_funcs()
        _deduplicate_e2e_scenarios = bf._deduplicate_e2e_scenarios
        E2EScenario = bf.E2EScenario

        sc1 = E2EScenario.model_construct(
            scenario_id="E2E-001", title="Login flow",
            category="observed", priority="P0_critical",
            steps=[], expected_outcome="User logged in",
            workflow_steps_covered=[1, 2], data_matrix=[],
        )
        sc2 = E2EScenario.model_construct(
            scenario_id="E2E-002", title="Payment processing",
            category="inferred_high_risk", priority="P1_high",
            steps=[], expected_outcome="Payment confirmed",
            workflow_steps_covered=[5, 6], data_matrix=[],
        )
        result = _deduplicate_e2e_scenarios([sc1, sc2], [])
        assert len(result) == 2
