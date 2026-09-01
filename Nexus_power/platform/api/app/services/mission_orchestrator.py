"""
Mission Orchestrator — coordinates engine calls for each mission stage.

This service is the brain behind mission stage execution. When a user
triggers a stage, the orchestrator:

 1. Reads the persona's stage_config to determine which engines to call
 2. Prepares inputs from accumulated mission context + stage inputs
 3. Calls each engine via httpx with JWT forwarding
 4. Collects results, creates artifacts, and updates stage outputs
 5. Merges results into mission context for the next stage

Engine integration follows the same pattern as the QA Orchestrator
(products/qa-orchestrator/main.py) — HTTP calls to engine endpoints
with standard NexusRequest/NexusResponse payloads.
"""
from __future__ import annotations

import time
import logging
from typing import Optional
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# ─── Engine URL Registry ──────────────────────────────────────

DEFAULT_ENGINE_URLS = {
    "ears": "http://ears-engine:8002",
    "eyes": "http://eyes-engine:8003",
    "heart": "http://heart-engine:8004",
    "backbone": "http://backbone-engine:8005",
    "nerves": "http://nerves-engine:8006",
    "legs": "http://legs-engine:8007",
    "hands": "http://hands-engine:8008",
    "spine": "http://spine-engine:8009",
    "mouth": "http://mouth-engine:8010",
    "shield": "http://shield-engine:8001",
    "brain": "http://brain-engine:8011",
}


# ─── Stage Engine Mapping ─────────────────────────────────────

STAGE_ENGINE_ACTIONS = {
    "capture": {
        "spine": {
            "description": "Document ingestion and processing",
            "endpoints": ["/api/v1/spine/ingest"],
        },
        "shield": {
            "description": "PII detection and redaction",
            "endpoints": ["/api/v1/shield/scan"],
        },
        "ears": {
            "description": "Audio transcription and speaker diarization",
            "endpoints": ["/api/v1/ears/transcribe"],
        },
        "eyes": {
            "description": "Visual analysis and screen recording processing",
            "endpoints": ["/api/v1/eyes/analyze-video"],
        },
    },
    "understand": {
        "heart": {
            "description": "Business rule extraction and analysis",
            "endpoints": ["/api/v1/heart/extract-rules", "/api/v1/heart/analyze"],
        },
        "backbone": {
            "description": "Knowledge graph construction and querying",
            "endpoints": ["/api/v1/backbone/nodes"],
        },
        "nerves": {
            "description": "Gap analysis and contradiction detection",
            "endpoints": ["/api/v1/nerves/connectors"],
        },
    },
    "strategize": {
        "heart": {
            "description": "Test strategy and flow exploration",
            "endpoints": ["/api/v1/heart/explore-flows"],
        },
        "nerves": {
            "description": "Impact analysis and prioritization",
            "endpoints": ["/api/v1/nerves/execute"],
        },
    },
    "generate": {
        "legs": {
            "description": "Test case generation and execution",
            "endpoints": ["/api/v1/legs/execute"],
        },
        "hands": {
            "description": "Test data profile generation",
            "endpoints": ["/api/v1/hands/generate-profiles"],
        },
        "mouth": {
            "description": "Report and documentation generation",
            "endpoints": ["/api/v1/mouth/generate"],
        },
    },
    "validate": {
        "legs": {
            "description": "Test execution and result collection",
            "endpoints": ["/api/v1/legs/execute"],
        },
        "nerves": {
            "description": "Traceability verification and compliance check",
            "endpoints": ["/api/v1/nerves/execute"],
        },
    },
}


@dataclass
class EngineCallResult:
    """Result of a single engine call."""
    engine: str
    endpoint: str
    status: str  # ok, error, timeout, skipped
    duration_ms: float = 0.0
    response_data: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "endpoint": self.endpoint,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


@dataclass
class StageExecutionResult:
    """Result of executing all engine calls for a stage."""
    stage_type: str
    engine_calls: list[EngineCallResult] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None

    @property
    def total_duration_ms(self) -> float:
        return sum(c.duration_ms for c in self.engine_calls)


class MissionOrchestrator:
    """
    Orchestrates engine calls for mission stages.

    Usage:
        orchestrator = MissionOrchestrator(http_client, engine_urls)
        result = await orchestrator.execute_stage(
            stage_type="capture",
            persona_stage_config={"engines": ["spine", "shield"]},
            mission_context={...},
            stage_inputs={...},
            tenant_id="t-1",
            auth_token="Bearer ...",
        )
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        engine_urls: Optional[dict[str, str]] = None,
    ):
        self._http = http_client
        self._engine_urls = engine_urls or DEFAULT_ENGINE_URLS

    async def execute_stage(
        self,
        stage_type: str,
        persona_stage_config: dict,
        mission_context: dict,
        stage_inputs: dict,
        tenant_id: str,
        auth_token: Optional[str] = None,
    ) -> StageExecutionResult:
        """
        Execute all engine calls for a given stage.

        Args:
            stage_type: One of capture/understand/strategize/generate/validate
            persona_stage_config: Stage config from persona (engines list, etc.)
            mission_context: Accumulated context from prior stages
            stage_inputs: Direct inputs for this stage
            tenant_id: Tenant ID for engine calls
            auth_token: JWT token to forward to engines

        Returns:
            StageExecutionResult with all engine call results and stage outputs
        """
        result = StageExecutionResult(stage_type=stage_type)
        engines_to_call = persona_stage_config.get("engines", [])
        stage_actions = STAGE_ENGINE_ACTIONS.get(stage_type, {})

        for engine_name in engines_to_call:
            if engine_name not in self._engine_urls:
                result.engine_calls.append(EngineCallResult(
                    engine=engine_name,
                    endpoint="",
                    status="skipped",
                    error=f"Unknown engine: {engine_name}",
                ))
                continue

            actions = stage_actions.get(engine_name, {})
            endpoints = actions.get("endpoints", [])

            if not endpoints:
                result.engine_calls.append(EngineCallResult(
                    engine=engine_name,
                    endpoint="",
                    status="skipped",
                    error=f"No endpoints configured for {engine_name} in {stage_type} stage",
                ))
                continue

            # Call each endpoint for this engine
            for endpoint in endpoints:
                call_result = await self._call_engine(
                    engine_name=engine_name,
                    endpoint=endpoint,
                    stage_type=stage_type,
                    mission_context=mission_context,
                    stage_inputs=stage_inputs,
                    tenant_id=tenant_id,
                    auth_token=auth_token,
                )
                result.engine_calls.append(call_result)

                # Merge successful responses into outputs
                if call_result.status == "ok" and call_result.response_data:
                    result.outputs[f"{engine_name}_{endpoint.split('/')[-1]}"] = call_result.response_data

        # Check for complete failure
        failed = [c for c in result.engine_calls if c.status == "error"]
        if failed and len(failed) == len([c for c in result.engine_calls if c.status != "skipped"]):
            result.success = False
            result.error = f"All engine calls failed for {stage_type} stage"

        return result

    async def _call_engine(
        self,
        engine_name: str,
        endpoint: str,
        stage_type: str,
        mission_context: dict,
        stage_inputs: dict,
        tenant_id: str,
        auth_token: Optional[str] = None,
    ) -> EngineCallResult:
        """Make a single engine HTTP call with timing."""
        base_url = self._engine_urls[engine_name]
        url = f"{base_url}{endpoint}"

        headers = {"Content-Type": "application/json"}
        if auth_token:
            headers["Authorization"] = auth_token

        # Build the request payload based on stage type and engine
        payload = self._build_payload(
            engine_name=engine_name,
            stage_type=stage_type,
            mission_context=mission_context,
            stage_inputs=stage_inputs,
            tenant_id=tenant_id,
        )

        start_time = time.monotonic()
        try:
            response = await self._http.post(url, json=payload, headers=headers, timeout=60.0)
            duration_ms = (time.monotonic() - start_time) * 1000

            if response.status_code < 400:
                return EngineCallResult(
                    engine=engine_name,
                    endpoint=endpoint,
                    status="ok",
                    duration_ms=duration_ms,
                    response_data=response.json() if response.content else {},
                )
            else:
                return EngineCallResult(
                    engine=engine_name,
                    endpoint=endpoint,
                    status="error",
                    duration_ms=duration_ms,
                    error=f"HTTP {response.status_code}: {response.text[:200]}",
                )
        except httpx.TimeoutException:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.warning(
                "engine_call.timeout",
                extra={"engine": engine_name, "endpoint": endpoint, "duration_ms": duration_ms},
            )
            return EngineCallResult(
                engine=engine_name,
                endpoint=endpoint,
                status="timeout",
                duration_ms=duration_ms,
                error=f"Timeout after {duration_ms:.0f}ms",
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - start_time) * 1000
            logger.error(
                "engine_call.error",
                extra={"engine": engine_name, "endpoint": endpoint, "error": str(exc)},
            )
            return EngineCallResult(
                engine=engine_name,
                endpoint=endpoint,
                status="error",
                duration_ms=duration_ms,
                error=str(exc),
            )

    def _build_payload(
        self,
        engine_name: str,
        stage_type: str,
        mission_context: dict,
        stage_inputs: dict,
        tenant_id: str,
    ) -> dict:
        """Build engine-specific request payload based on stage type.

        Each engine expects a different payload shape. This method adapts
        mission context into the format each engine expects.
        """
        import uuid
        base = {
            "tenant_id": tenant_id,
            "trace_id": str(uuid.uuid4()),
        }

        # Forward canonical artifact ID from prior stages when available
        artifact_id = mission_context.get("canonical_artifact_id")
        if artifact_id:
            base["artifact_id"] = artifact_id

        # Merge mission context items that are relevant
        transcript = mission_context.get("transcript", "")
        # Also check stage-prefixed keys from _execute_mission_stage_bg
        if not transcript:
            for k, v in mission_context.items():
                if "transcript" in k and isinstance(v, str):
                    transcript = v
                    break
        rules = mission_context.get("rules", [])
        documents = mission_context.get("documents", [])

        if engine_name == "shield":
            return {
                **base,
                "text": stage_inputs.get("text", transcript or ""),
            }
        elif engine_name == "heart":
            if "extract-rules" in str(stage_inputs.get("_endpoint", "")):
                return {
                    **base,
                    "transcript": stage_inputs.get("transcript", transcript or ""),
                    "session_id": stage_inputs.get("session_id", str(uuid.uuid4())),
                }
            return {
                **base,
                "content": stage_inputs.get("content", transcript or ""),
            }
        elif engine_name == "backbone":
            return {
                **base,
                "node_type": stage_inputs.get("node_type", "business_rule"),
                "properties": stage_inputs.get("properties", {}),
            }
        elif engine_name == "hands":
            return {
                **base,
                "count": stage_inputs.get("count", 10),
                "config": stage_inputs.get("config", {}),
            }
        elif engine_name == "legs":
            return {
                **base,
                "test_case": stage_inputs.get("test_case", {}),
            }
        elif engine_name == "mouth":
            return {
                **base,
                "report_type": stage_inputs.get("report_type", "summary"),
                "data": stage_inputs.get("data", {"rules": rules}),
            }
        elif engine_name == "nerves":
            return {
                **base,
                "connector": stage_inputs.get("connector", "analysis"),
                "action": stage_inputs.get("action", "check"),
                "parameters": stage_inputs.get("parameters", {}),
            }
        else:
            # Generic fallback
            return {**base, **stage_inputs}

    async def check_engine_health(self, engine_name: str) -> dict:
        """Quick health check for a specific engine."""
        if engine_name not in self._engine_urls:
            return {"engine": engine_name, "status": "unknown", "error": "Not configured"}

        base_url = self._engine_urls[engine_name]
        try:
            response = await self._http.get(f"{base_url}/health/ready", timeout=5.0)
            if response.status_code == 200:
                return {"engine": engine_name, "status": "healthy", **response.json()}
            return {"engine": engine_name, "status": "degraded"}
        except Exception as exc:
            return {"engine": engine_name, "status": "unreachable", "error": str(exc)}

    async def check_stage_readiness(
        self,
        stage_type: str,
        persona_stage_config: dict,
    ) -> dict:
        """Check if all engines needed for a stage are available."""
        engines_needed = persona_stage_config.get("engines", [])
        health_checks = {}
        all_ready = True

        for engine in engines_needed:
            health = await self.check_engine_health(engine)
            health_checks[engine] = health
            if health.get("status") != "healthy":
                all_ready = False

        return {
            "stage_type": stage_type,
            "ready": all_ready,
            "engines": health_checks,
        }
