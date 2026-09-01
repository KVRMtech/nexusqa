"""
Brain Engine — Unit tests.

Tests BrainConfig, BrainLLM stub, request/response models,
QualityGate, SessionReasoner, TierManager, and DecisionEngine.
"""

import pytest
import sys
import os
import json
import importlib

_BRAIN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "engines", "brain-engine")
_SDK_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "sdk", "nexus-sdk")


def _ensure_brain_imports():
    """Ensure brain-engine's main.py & app/ are importable (not another engine's)."""
    # Clear cached modules from other engine tests
    for mod_name in list(sys.modules.keys()):
        if mod_name == "main" or mod_name.startswith("app.") or mod_name == "app":
            del sys.modules[mod_name]
    # Ensure brain-engine is at the front of sys.path
    if _BRAIN_DIR not in sys.path:
        sys.path.insert(0, _BRAIN_DIR)
    else:
        sys.path.remove(_BRAIN_DIR)
        sys.path.insert(0, _BRAIN_DIR)
    if _SDK_DIR not in sys.path:
        sys.path.insert(0, _SDK_DIR)


@pytest.fixture(autouse=True)
def _brain_env():
    """Fixture that ensures brain-engine modules are loaded for every test."""
    _ensure_brain_imports()
    yield
    # No cleanup needed — next test will re-clear


# ─── Config ────────────────────────────────────────────────────

class TestBrainConfig:

    def test_defaults(self):
        from main import BrainConfig
        cfg = BrainConfig()
        assert cfg.engine_name == "brain"
        assert cfg.engine_port == 8011
        assert cfg.quality_pass_threshold == 0.6
        assert cfg.escalation_threshold == 0.4
        assert cfg.ollama_model == "llama3.2:1b"
        assert cfg.llm_temperature == 0.1
        assert cfg.llm_max_tokens == 4096

    def test_service_scoped_model_overrides_global(self, monkeypatch):
        from main import BrainConfig
        monkeypatch.setenv("OLLAMA_MODEL", "wrong-global")
        monkeypatch.setenv("BRAIN_OLLAMA_MODEL", "brain-local")
        cfg = BrainConfig()
        assert cfg.ollama_model == "brain-local"


# ─── BrainLLM Stub ────────────────────────────────────────────


class TestBrainLLMStub:
    """Test the development stub that returns structured JSON decisions."""

    def test_quality_stub(self):
        from main import BrainLLM
        raw = BrainLLM._stub_generate(
            "Evaluate quality of the session outputs",
            "5 rules, 10 tests",
        )
        parsed = json.loads(raw)
        assert "action" in parsed
        assert parsed["action"] == "needs_review"
        assert "confidence" in parsed
        assert "requires_human" in parsed

    def test_routing_stub(self):
        from main import BrainLLM
        raw = BrainLLM._stub_generate(
            "Which engine should process next? Route the request.",
            "Session has transcript but no rules",
        )
        parsed = json.loads(raw)
        assert parsed["action"] == "route"
        assert "recommended_engines" in parsed

    def test_merge_stub(self):
        from main import BrainLLM
        raw = BrainLLM._stub_generate(
            "Merge and reconcile results from multiple engines",
            "Heart says A, Shield says B",
        )
        parsed = json.loads(raw)
        assert parsed["action"] == "merged"

    def test_generic_stub(self):
        from main import BrainLLM
        raw = BrainLLM._stub_generate(
            "Unknown type of prompt",
            "Some random question",
        )
        parsed = json.loads(raw)
        assert "action" in parsed
        assert "confidence" in parsed


# ─── Request/Response Models ──────────────────────────────────


class TestRequestModels:

    def test_decide_request_required_fields(self):
        from main import DecideRequest
        req = DecideRequest(
            tenant_id="t1",
            session_id="sess-001",
            decision_type="route",
        )
        assert req.session_id == "sess-001"
        assert req.decision_type == "route"
        assert req.engine_results == {}
        assert req.rules == []
        assert req.test_cases == []

    def test_quality_gate_request(self):
        from main import QualityGateRequest
        req = QualityGateRequest(
            tenant_id="t1",
            session_id="sess-002",
            rules=[{"rule_id": "r1", "rule_text": "IF x THEN y"}],
            test_cases=[{"test_id": "tc1", "name": "Happy path"}],
        )
        assert len(req.rules) == 1
        assert len(req.test_cases) == 1
        assert req.pii_result is None

    def test_session_update_request(self):
        from main import SessionUpdateRequest
        req = SessionUpdateRequest(
            tenant_id="t1",
            session_id="sess-003",
            engine_name="heart",
            result={"rules": [{"rule_id": "r1"}]},
        )
        assert req.engine_name == "heart"
        assert "rules" in req.result

    def test_ask_brain_request(self):
        from main import AskBrainRequest
        req = AskBrainRequest(
            tenant_id="t1",
            question="What engines are needed for audio QA?",
        )
        assert "audio" in req.question
        assert req.session_id is None
        assert req.context is None


class TestResponseModels:

    def test_decide_response(self):
        from main import DecideResponse
        resp = DecideResponse(
            success=True,
            trace_id="t-001",
            engine="brain",
            engine_version="1.0.0",
            decision_id="d-001",
            decision_type="route",
            action="route_to_heart",
            reasoning="Session needs rule extraction",
            confidence=0.85,
            recommended_engines=["heart", "shield"],
        )
        assert resp.decision_id == "d-001"
        assert resp.confidence == 0.85
        assert "heart" in resp.recommended_engines
        assert not resp.requires_human

    def test_quality_gate_response(self):
        from main import QualityGateResponse
        resp = QualityGateResponse(
            success=True,
            trace_id="t-002",
            engine="brain",
            engine_version="1.0.0",
            session_id="sess-001",
            overall_score=0.78,
            level="good",
            passed=True,
            rule_completeness=0.8,
            test_coverage=0.75,
            consistency=0.7,
            confidence_avg=0.85,
            pii_safety=1.0,
        )
        assert resp.passed is True
        assert resp.overall_score == 0.78
        assert resp.level == "good"

    def test_tier_status_response(self):
        from main import TierStatusResponse
        resp = TierStatusResponse(
            success=True,
            trace_id="t-003",
            engine="brain",
            engine_version="1.0.0",
            overall_mode="on-prem",
            total_engines=11,
            onprem_engines=["heart", "brain"],
        )
        assert resp.overall_mode == "on-prem"
        assert resp.total_engines == 11


# ─── Quality Gate ─────────────────────────────────────────────


class TestQualityGate:

    def test_empty_session_fails(self):
        from app.coordinator.quality_gate import QualityGate
        gate = QualityGate(pass_threshold=0.6)
        score = gate.evaluate(rules=[], test_cases=[], engine_results={})
        assert not score.passed
        assert score.overall < 0.6
        assert score.level.value == "poor"

    def test_minimal_session_scores(self):
        from app.coordinator.quality_gate import QualityGate
        gate = QualityGate(pass_threshold=0.6)
        rules = [
            {"rule_id": "r1", "rule_text": "IF x THEN y", "source_reference": "doc.pdf"},
            {"rule_id": "r2", "rule_text": "IF a THEN b"},
        ]
        tests = [
            {"test_id": "tc1", "rule_id": "r1", "name": "happy path"},
        ]
        score = gate.evaluate(
            rules=rules,
            test_cases=tests,
            engine_results={"heart": {"rules": rules}},
            confidence_scores={"heart": 0.8},
        )
        assert 0.0 < score.overall <= 1.0
        assert score.rule_completeness > 0
        assert score.test_coverage > 0

    def test_excellent_session(self):
        from app.coordinator.quality_gate import QualityGate
        gate = QualityGate(pass_threshold=0.6)
        rules = [
            {"rule_id": f"r{i}", "rule_text": f"Rule {i}", "source_reference": f"doc{i}.pdf"}
            for i in range(10)
        ]
        tests = [
            {"test_id": f"tc{i}", "rule_id": f"r{i}", "name": f"Test {i}"}
            for i in range(10)
        ]
        score = gate.evaluate(
            rules=rules,
            test_cases=tests,
            engine_results={
                "heart": {"rules": rules, "test_cases": tests},
                "shield": {"pii_detected": False},
                "ears": {"transcript": "full transcript"},
            },
            confidence_scores={"heart": 0.95, "shield": 0.99, "ears": 0.9},
            pii_result={"pii_detected": False, "entities": []},
        )
        assert score.passed
        assert score.overall >= 0.6

    def test_quality_levels(self):
        from app.coordinator.quality_gate import QualityLevel
        assert QualityLevel.EXCELLENT.value == "excellent"
        assert QualityLevel.POOR.value == "poor"


# ─── Session Reasoner ─────────────────────────────────────────


class TestSessionReasoner:

    def test_create_session(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        state = sr.get_or_create("sess-100", "tenant-1")
        assert state.session_id == "sess-100"
        assert state.tenant_id == "tenant-1"
        assert len(state.engines_completed) == 0

    def test_update_from_heart(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("sess-101", "t1")
        updated = sr.update_from_engine(
            "sess-101", "heart",
            {"rules": [{"id": "r1"}], "test_cases": [{"id": "tc1"}]},
        )
        assert "heart" in updated.engines_completed
        assert len(updated.rules) == 1
        assert len(updated.test_cases) == 1

    def test_update_from_shield(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("sess-102", "t1")
        updated = sr.update_from_engine(
            "sess-102", "shield",
            {"entities": [{"type": "EMAIL"}], "safe_text": "***@***.com"},
        )
        assert "shield" in updated.engines_completed
        assert len(updated.pii_entities) == 1
        assert updated.redacted_text == "***@***.com"

    def test_update_from_ears(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("sess-103", "t1")
        updated = sr.update_from_engine(
            "sess-103", "ears",
            {"transcript": "Hello world", "segments": [{"text": "Hello"}]},
        )
        assert "ears" in updated.engines_completed
        assert updated.transcript == "Hello world"

    def test_analyze_gaps_empty(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("sess-104", "t1")
        gaps = sr.analyze_gaps("sess-104")
        assert "gaps" in gaps
        assert len(gaps["gaps"]) > 0
        assert gaps["completeness"] == 0.0

    def test_analyze_gaps_missing_session(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        result = sr.analyze_gaps("nonexistent")
        assert "error" in result

    def test_list_sessions(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("s1", "t1")
        sr.get_or_create("s2", "t1")
        sessions = sr.list_sessions()
        assert len(sessions) == 2

    def test_completeness(self):
        from app.coordinator.session_reasoner import SessionReasoner
        sr = SessionReasoner()
        sr.get_or_create("sess-105", "t1")
        sr.update_from_engine("sess-105", "ears", {"transcript": "Hi"})
        sr.update_from_engine("sess-105", "shield", {"pii_entities": []})
        sr.update_from_engine("sess-105", "heart", {"rules": [{"id": "r1"}]})
        state = sr.get_session("sess-105")
        completeness = state.completeness()
        assert 0 < completeness <= 1.0


# ─── Tier Manager ──────────────────────────────────────────────


class TestTierManager:

    def test_default_tier_map(self):
        from app.tier_manager.manager import TierManager, RUNTIME_LLM_TIERS, PLANNED_LLM_TIERS, TOOLING_RECOMMENDATIONS
        # Active LLM engines in RUNTIME_LLM_TIERS (only brain + heart are wired)
        assert "brain" in RUNTIME_LLM_TIERS
        assert "heart" in RUNTIME_LLM_TIERS
        assert len(RUNTIME_LLM_TIERS) == 2
        # Planned LLM engines (not yet wired into engine code)
        assert "eyes" in PLANNED_LLM_TIERS
        assert "hands" in PLANNED_LLM_TIERS
        assert "mouth" in PLANNED_LLM_TIERS
        assert "spine" in PLANNED_LLM_TIERS
        assert len(PLANNED_LLM_TIERS) == 4
        # Non-LLM engines in TOOLING_RECOMMENDATIONS
        assert "ears" in TOOLING_RECOMMENDATIONS
        assert "shield" in TOOLING_RECOMMENDATIONS
        assert "backbone" in TOOLING_RECOMMENDATIONS
        assert "legs" in TOOLING_RECOMMENDATIONS
        assert "nerves" in TOOLING_RECOMMENDATIONS
        assert len(TOOLING_RECOMMENDATIONS) == 5
        # Combined = 11 engines
        assert len(RUNTIME_LLM_TIERS) + len(PLANNED_LLM_TIERS) + len(TOOLING_RECOMMENDATIONS) == 11

    def test_tier_map_has_three_tiers(self):
        from app.tier_manager.manager import RUNTIME_LLM_TIERS, PLANNED_LLM_TIERS, TOOLING_RECOMMENDATIONS
        for engine, tiers in {**RUNTIME_LLM_TIERS, **PLANNED_LLM_TIERS}.items():
            assert "tier1" in tiers, f"{engine} missing tier1"
            assert "tier2" in tiers, f"{engine} missing tier2"
            assert "tier3" in tiers, f"{engine} missing tier3"
        for engine, tiers in TOOLING_RECOMMENDATIONS.items():
            assert "tier1" in tiers, f"{engine} missing tier1"
            assert "tier2" in tiers, f"{engine} missing tier2"
            assert "tier3" in tiers, f"{engine} missing tier3"

    def test_detect_active_tiers_no_env(self):
        """With no env vars set, should detect on-prem/stub mode."""
        from app.tier_manager.manager import TierManager
        tm = TierManager()
        tm.detect_active_tiers()
        summary = tm.get_deployment_summary()
        assert summary["total_engines"] == 11
        assert summary["overall_mode"] in ("on-prem", "cloud", "hybrid", "full-on-prem")

    def test_get_recommended_tiers(self):
        from app.tier_manager.manager import TierManager
        tm = TierManager()
        recommended = tm.get_recommended_tiers()
        assert "brain" in recommended
        assert "tier1" in recommended["brain"]

    def test_get_engine_tiers_known(self):
        from app.tier_manager.manager import TierManager
        tm = TierManager()
        tm.detect_active_tiers()
        tiers = tm.get_engine_tiers("brain")
        assert tiers is not None

    def test_get_engine_tiers_unknown(self):
        from app.tier_manager.manager import TierManager
        tm = TierManager()
        tm.detect_active_tiers()
        tiers = tm.get_engine_tiers("nonexistent_engine")
        assert tiers is None


# ─── Decision Engine ──────────────────────────────────────────


class TestDecisionEngine:

    def test_decision_types(self):
        from app.coordinator.decision_engine import DecisionType
        assert DecisionType.ROUTE.value == "route"
        assert DecisionType.QUALITY_GATE.value == "quality_gate"
        assert DecisionType.MERGE.value == "merge"
        assert DecisionType.SUMMARIZE.value == "summarize"

    def test_decision_context_creation(self):
        from app.coordinator.decision_engine import DecisionContext, DecisionType
        ctx = DecisionContext(
            session_id="sess-001",
            tenant_id="t1",
            decision_type=DecisionType.ROUTE,
            engine_results={"heart": {"rules": []}},
        )
        assert ctx.session_id == "sess-001"
        assert ctx.decision_type == DecisionType.ROUTE

    @pytest.mark.asyncio
    async def test_route_decision(self):
        """Test routing decision with a mock LLM that returns JSON."""
        from app.coordinator.decision_engine import DecisionEngine, DecisionContext, DecisionType

        async def mock_llm(system, user, **kw):
            return json.dumps({
                "action": "route_to_heart",
                "reasoning": "Need rule extraction",
                "confidence": 0.9,
                "recommended_engines": ["heart"],
                "parameters": {},
                "requires_human": False,
            })

        engine = DecisionEngine(llm_generate_fn=mock_llm)
        ctx = DecisionContext(
            session_id="s1",
            tenant_id="t1",
            decision_type=DecisionType.ROUTE,
            engine_results={},
        )
        decision = await engine.decide(ctx)
        assert decision.action == "route_to_heart"
        assert decision.confidence == 0.9
        assert "heart" in decision.recommended_engines

    @pytest.mark.asyncio
    async def test_quality_gate_decision(self):
        from app.coordinator.decision_engine import DecisionEngine, DecisionContext, DecisionType

        async def mock_llm(system, user, **kw):
            return json.dumps({
                "action": "pass",
                "reasoning": "All quality criteria met",
                "confidence": 0.95,
                "requires_human": False,
            })

        engine = DecisionEngine(llm_generate_fn=mock_llm)
        ctx = DecisionContext(
            session_id="s1",
            tenant_id="t1",
            decision_type=DecisionType.QUALITY_GATE,
            rules_extracted=[{"id": "r1"}],
            test_cases=[{"id": "tc1"}],
        )
        decision = await engine.decide(ctx)
        assert decision.action == "pass"
        assert decision.confidence >= 0.9

    @pytest.mark.asyncio
    async def test_merge_decision(self):
        from app.coordinator.decision_engine import DecisionEngine, DecisionContext, DecisionType

        async def mock_llm(system, user, **kw):
            return json.dumps({
                "action": "merged",
                "reasoning": "Results merged without conflicts",
                "confidence": 0.85,
                "requires_human": False,
            })

        engine = DecisionEngine(llm_generate_fn=mock_llm)
        ctx = DecisionContext(
            session_id="s1",
            tenant_id="t1",
            decision_type=DecisionType.MERGE,
            engine_results={"heart": {"rules": []}, "shield": {"pii": []}},
        )
        decision = await engine.decide(ctx)
        assert decision.action == "merged"

    @pytest.mark.asyncio
    async def test_fallback_on_bad_json(self):
        """When LLM returns non-JSON, decision engine handles gracefully."""
        from app.coordinator.decision_engine import DecisionEngine, DecisionContext, DecisionType

        async def mock_llm(system, user, **kw):
            return "I think you should route to heart engine for rules"

        engine = DecisionEngine(llm_generate_fn=mock_llm)
        ctx = DecisionContext(
            session_id="s1",
            tenant_id="t1",
            decision_type=DecisionType.ROUTE,
            engine_results={},
        )
        decision = await engine.decide(ctx)
        assert decision is not None
        assert decision.action != ""
        assert 0.0 <= decision.confidence <= 1.0
