"""
Session Reasoner — Cross-engine reasoning about QA session state.

Maintains session context and provides intelligent analysis
across all engine outputs for a given QA session.

**P0: Durable state** — Sessions are persisted to Redis so that
Brain restarts and horizontal replicas share the same state.
Falls back to in-memory when Redis is unavailable (dev/test only).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Redis key prefix for Brain session state
_REDIS_PREFIX = "brain:session:"
_REDIS_INDEX_KEY = "brain:sessions"


@dataclass
class SessionState:
    """Aggregated state of a QA session across all engines."""
    session_id: str
    tenant_id: str
    created_at: float = field(default_factory=time.time)

    # Engine outputs
    transcript: str = ""
    transcript_speakers: list[str] = field(default_factory=list)
    pii_entities: list[dict] = field(default_factory=list)
    redacted_text: str = ""
    visual_elements: list[dict] = field(default_factory=list)
    visual_flows: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    test_cases: list[dict] = field(default_factory=list)
    test_results: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    knowledge_entries: list[dict] = field(default_factory=list)
    reports: list[dict] = field(default_factory=list)
    synthetic_data: list[dict] = field(default_factory=list)

    # Metadata
    engines_completed: list[str] = field(default_factory=list)
    engines_failed: list[str] = field(default_factory=list)
    confidence_scores: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def completeness(self) -> float:
        """How complete is this session? 0.0-1.0 based on data presence."""
        checks = [
            bool(self.transcript or self.redacted_text),
            bool(self.rules),
            bool(self.test_cases),
            len(self.engines_completed) >= 3,
        ]
        return sum(checks) / len(checks)

    # ── Serialisation helpers for Redis ────────────────────────

    def to_json(self) -> str:
        """Serialise to JSON string for Redis storage."""
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, raw: str) -> "SessionState":
        """Deserialise from JSON string."""
        data = json.loads(raw)
        return cls(**data)


class SessionReasoner:
    """
    Reasons about QA session state across all engines.

    Persistence strategy (P0 fix):
    - **Redis-first**: Every mutation is written to Redis immediately.
    - **Hot cache**: An in-memory dict mirrors Redis for fast reads.
    - **Startup reload**: On init, all existing sessions are loaded from
      Redis so that restarts / new replicas get full state.
    - **Fallback**: When Redis is unavailable the reasoner works
      in-memory only (acceptable for dev/test, flagged in production).

    Cross-engine analysis:
    - Which engines have contributed data
    - What gaps exist
    - What the next best action is
    - Confidence-weighted aggregation
    """

    def __init__(self, redis_client=None):
        """
        Args:
            redis_client: An *async* ``redis.asyncio.Redis`` instance.
                          Pass ``None`` for in-memory-only mode.
        """
        self._sessions: dict[str, SessionState] = {}
        self._redis = redis_client

    # ── Redis lifecycle ────────────────────────────────────────

    async def connect_redis(self, redis_url: str) -> bool:
        """Connect to Redis.  Returns True on success."""
        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("brain.session_reasoner: Redis connected (%s)", redis_url)
            return True
        except Exception as exc:
            logger.warning("brain.session_reasoner: Redis unavailable — in-memory fallback: %s", exc)
            self._redis = None
            return False

    async def load_from_redis(self) -> int:
        """Load all persisted sessions into the hot cache.  Returns count loaded."""
        if not self._redis:
            return 0
        try:
            session_ids = await self._redis.smembers(_REDIS_INDEX_KEY)
            loaded = 0
            for sid in session_ids:
                raw = await self._redis.get(f"{_REDIS_PREFIX}{sid}")
                if raw:
                    self._sessions[sid] = SessionState.from_json(raw)
                    loaded += 1
            logger.info("brain.session_reasoner: Loaded %d sessions from Redis", loaded)
            return loaded
        except Exception as exc:
            logger.warning("brain.session_reasoner: Redis load failed: %s", exc)
            return 0

    async def _persist(self, state: SessionState) -> None:
        """Write a single session to Redis (fire-and-forget-safe)."""
        if not self._redis:
            return
        try:
            key = f"{_REDIS_PREFIX}{state.session_id}"
            pipe = self._redis.pipeline()
            pipe.set(key, state.to_json())
            pipe.sadd(_REDIS_INDEX_KEY, state.session_id)
            await pipe.execute()
        except Exception as exc:
            logger.warning(
                "brain.session_reasoner: Redis persist failed for %s: %s",
                state.session_id, exc,
            )

    async def _delete_persisted(self, session_id: str) -> None:
        """Remove a single session from Redis."""
        if not self._redis:
            return
        try:
            pipe = self._redis.pipeline()
            pipe.delete(f"{_REDIS_PREFIX}{session_id}")
            pipe.srem(_REDIS_INDEX_KEY, session_id)
            await pipe.execute()
        except Exception as exc:
            logger.warning(
                "brain.session_reasoner: Redis delete failed for %s: %s",
                session_id, exc,
            )

    # ── Public session API (unchanged signatures) ──────────────

    def get_or_create(self, session_id: str, tenant_id: str) -> SessionState:
        """Get existing session state or create new."""
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                session_id=session_id,
                tenant_id=tenant_id,
            )
        return self._sessions[session_id]

    def get_session(self, session_id: str) -> Optional[SessionState]:
        """Get session state if it exists."""
        return self._sessions.get(session_id)

    def update_from_engine(
        self,
        session_id: str,
        engine_name: str,
        result: dict[str, Any],
    ) -> SessionState:
        """Update session state with results from an engine.

        P0 fix: Field mappings now match actual engine response shapes.
        The orchestrator sends the raw engine output — for polling jobs
        the inner ``result`` is already extracted, and for ``for_each``
        stages the output is ``{"items": [...], "count": N}``.
        """
        state = self._sessions.get(session_id)
        if not state:
            logger.warning(
                "brain.session.unknown", extra={"session_id": session_id, "engine": engine_name}
            )
            return SessionState(session_id=session_id, tenant_id="unknown")

        # Map engine results to session state — aligned with real response shapes
        if engine_name == "ears":
            # TranscriptionResult: segments[].text, speakers[].speaker_id
            # May also have a "transcript" or "full_text" convenience key
            state.transcript = result.get("transcript", result.get("full_text", ""))
            if not state.transcript and "segments" in result:
                # Join segment texts into a full transcript
                state.transcript = " ".join(
                    seg.get("text", "") for seg in result.get("segments", [])
                )
            speakers = result.get("speakers", [])
            if speakers and isinstance(speakers[0], dict):
                state.transcript_speakers = [
                    s.get("speaker_id", s.get("speaker", "")) for s in speakers
                ]
            elif speakers and isinstance(speakers[0], str):
                state.transcript_speakers = speakers

        elif engine_name == "shield":
            state.pii_entities = result.get("entities", state.pii_entities)
            state.redacted_text = result.get("safe_text", state.redacted_text)

        elif engine_name == "eyes":
            # VisualAnalysisResult: frames[].ui_elements, no top-level "ui_elements"
            frames = result.get("frames", [])
            if frames:
                # Flatten all ui_elements from all frames
                state.visual_elements = [
                    elem
                    for frame in frames
                    for elem in frame.get("ui_elements", [])
                ]
                # Use frames as visual flows (each frame = a step in the flow)
                state.visual_flows = frames
            else:
                # Direct fallback if engine sends flattened data
                state.visual_elements = result.get("ui_elements", state.visual_elements)
                state.visual_flows = result.get("flows", state.visual_flows)

        elif engine_name == "heart":
            state.rules = result.get("rules", state.rules)
            state.test_cases = result.get("test_cases", state.test_cases)

        elif engine_name == "backbone":
            # For for_each stages: {"items": [...rule_responses...], "count": N}
            # For single calls: {"rule_id": "...", "node_id": "...", "related_rules": [...]}
            if "items" in result:
                state.knowledge_entries = result.get("items", [])
            elif "node_id" in result or "rule_id" in result:
                # Single rule storage response — append to existing entries
                state.knowledge_entries.append({
                    "rule_id": result.get("rule_id", ""),
                    "node_id": result.get("node_id", ""),
                    "related_rules": result.get("related_rules", []),
                })
            else:
                state.knowledge_entries = result.get("entries", state.knowledge_entries)

        elif engine_name == "hands":
            state.synthetic_data = result.get("profiles", state.synthetic_data)

        elif engine_name == "legs":
            # For for_each stages: {"items": [...test_results...], "count": N}
            # For single calls: {"result": {test execution result}}
            if "items" in result:
                state.test_results = result.get("items", [])
            elif "result" in result and isinstance(result["result"], dict):
                state.test_results.append(result["result"])
            else:
                state.test_results = result.get("results", state.test_results)

        elif engine_name == "spine":
            # For for_each stages: {"items": [...doc_results...], "count": N}
            if "items" in result:
                state.documents = result.get("items", [])
            else:
                state.documents = result.get("documents", state.documents)

        elif engine_name == "mouth":
            # POST response: {report_id, status, report_type, format}
            # Wrap into a list entry for session tracking
            if "report_id" in result:
                state.reports.append({
                    "report_id": result.get("report_id"),
                    "status": result.get("status"),
                    "report_type": result.get("report_type"),
                    "format": result.get("format"),
                })
            elif "items" in result:
                state.reports = result.get("items", [])
            else:
                state.reports = result.get("reports", state.reports)

        # Track completion
        if engine_name not in state.engines_completed:
            state.engines_completed.append(engine_name)

        # Update confidence
        confidence = result.get("confidence")
        if confidence is not None:
            state.confidence_scores[engine_name] = float(confidence)

        logger.info(
            "brain.session.updated",
            extra={
                "session_id": session_id,
                "engine": engine_name,
                "completeness": state.completeness(),
            },
        )
        return state

    async def update_from_engine_durable(
        self,
        session_id: str,
        engine_name: str,
        result: dict[str, Any],
    ) -> SessionState:
        """Same as update_from_engine but also persists to Redis."""
        state = self.update_from_engine(session_id, engine_name, result)
        await self._persist(state)
        return state

    def analyze_gaps(self, session_id: str) -> dict[str, Any]:
        """Analyze what's missing from a session."""
        state = self._sessions.get(session_id)
        if not state:
            return {"error": "Session not found"}

        gaps = []
        recommendations = []

        if not state.transcript and not state.redacted_text:
            gaps.append("No transcript data — run Ears engine")
            recommendations.append("ears")
        if not state.redacted_text and state.transcript:
            gaps.append("Transcript not redacted — run Shield engine")
            recommendations.append("shield")
        if not state.rules:
            gaps.append("No business rules extracted — run Heart engine")
            recommendations.append("heart")
        if not state.test_cases and state.rules:
            gaps.append("Rules found but no test cases — run Heart test generation")
            recommendations.append("heart")
        if state.rules and not state.knowledge_entries:
            gaps.append("Rules not stored in knowledge graph — run Backbone engine")
            recommendations.append("backbone")
        if state.test_cases and not state.test_results:
            gaps.append("Test cases not executed — run Legs engine")
            recommendations.append("legs")
        if state.rules and not state.reports:
            gaps.append("No report generated — run Mouth engine")
            recommendations.append("mouth")

        return {
            "session_id": session_id,
            "completeness": state.completeness(),
            "engines_completed": state.engines_completed,
            "gaps": gaps,
            "recommended_next": recommendations,
            "warnings": state.warnings,
        }

    def list_sessions(self) -> list[dict]:
        """List all tracked sessions."""
        return [
            {
                "session_id": s.session_id,
                "tenant_id": s.tenant_id,
                "completeness": s.completeness(),
                "engines_completed": s.engines_completed,
                "rules_count": len(s.rules),
                "tests_count": len(s.test_cases),
            }
            for s in self._sessions.values()
        ]

    async def remove_session(self, session_id: str) -> bool:
        """Remove session state from cache and Redis."""
        removed = self._sessions.pop(session_id, None) is not None
        if removed:
            await self._delete_persisted(session_id)
        return removed
