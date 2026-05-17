"""
Nexus Shield Engine v0.2.0 — PII Detection & Redaction.

The Shield is the FIRST engine that processes any data.
Nothing reaches Ears, Eyes, Heart, or Backbone without
passing through Shield first.

Capabilities:
1. Standard PII detection (SSN, names, DOB, phone, email, addresses)
2. Insurance-domain PII (policy numbers, agent NPNs, MIB codes)
3. Context-aware PII detection (indirect references to people)
4. Reversible redaction (encrypted mapping for authorized de-redaction)
5. Audit logging (every PII touch is recorded)

v0.2.0 — Modular refactor:
  app.detectors  → PIIDetector, PIIType (regex pattern engine)
  app.redactors  → RedactionStore, PIIRedactor, ShieldAuditLog
"""

from __future__ import annotations

import os
import re
import logging
import time
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from nexus_sdk import NexusEngine, EngineConfig
from nexus_sdk.models import NexusRequest, NexusResponse, SourceReference
from nexus_sdk.auth import NexusUser, get_current_user
from nexus_sdk.events import NexusEvent

# ── Modular sub-packages ───────────────────────────────────────
from app.detectors import PIIType, PIIDetector
from app.redactors import RedactionStore, PIIRedactor, ShieldAuditLog

logger = logging.getLogger(__name__)


# ─── Configuration ─────────────────────────────────────────────

class ShieldConfig(EngineConfig):
    engine_name: str = "shield"
    engine_port: int = 8001
    shield_pii_confidence_threshold: float = 0.85
    shield_mapping_ttl_seconds: int = 86400 * 30  # 30 days default
    shield_encryption_key: str = ""  # Fernet key; auto-generated if empty


# ─── Request/Response Models ──────────────────────────────────

class RedactRequest(NexusRequest):
    text: str = Field(..., description="Text to redact")
    custom_patterns: Optional[dict[str, str]] = Field(
        default=None, description="Additional regex patterns {name: pattern}"
    )


class RedactResponse(NexusResponse):
    safe_text: str = Field(..., description="Redacted text")
    mapping_id: str = Field(..., description="ID to reverse the redaction")
    entities_found: list[dict] = Field(default_factory=list, description="PII entities detected")
    entity_count: int = Field(default=0, description="Total PII entities found")


class RevealRequest(BaseModel):
    mapping_id: str = Field(..., description="Mapping ID from redaction")
    tenant_id: str = Field(..., description="Tenant ID for authorization")


class RevealResponse(BaseModel):
    original_text: str


class AnalyzeRequest(NexusRequest):
    text: str = Field(..., description="Text to analyze for PII (no redaction)")


class AnalyzeResponse(NexusResponse):
    entities: list[dict] = Field(default_factory=list)
    risk_level: str = Field(default="low", description="low, medium, high, critical")


# ─── The Shield Engine v0.2.0 ─────────────────────────────────

class ShieldEngine(NexusEngine):
    def __init__(self):
        self.cfg = ShieldConfig()
        super().__init__(
            name="shield",
            version="0.2.0",
            config=self.cfg,
            description="PII Detection & Redaction Engine",
        )
        self.detector = PIIDetector()
        self._redaction_store = RedactionStore(self.cfg)
        self.redactor = PIIRedactor(self._redaction_store)

    async def on_startup(self):
        """Initialize Shield components and load PII patterns from plugins."""
        await self._redaction_store.connect()

        await ShieldAuditLog.connect(
            redis_host=self.cfg.redis_host,
            redis_port=self.cfg.redis_port,
            redis_password=self.cfg.redis_password,
        )

        # Load additional PII patterns from domain plugins
        try:
            pii_ext = self.plugin_registry.get_merged_pii()
            if pii_ext and pii_ext.entity_definitions:
                for entity_def in pii_ext.entity_definitions:
                    if entity_def.patterns:
                        for pattern in entity_def.patterns:
                            if entity_def.entity_type not in self.detector.PATTERNS:
                                self.detector.PATTERNS[entity_def.entity_type] = []
                            self.detector.PATTERNS[entity_def.entity_type].append(
                                re.compile(pattern, re.IGNORECASE)
                            )
        except Exception:
            pass

        self.health.set_mode(
            "redaction_store",
            "redis" if self._redaction_store._redis else "in-memory",
        )

        if self.event_bus:
            await self.event_bus.subscribe(
                "ears.transcription.completed", self._handle_transcription
            )

        # ── Canonical workflow workers (Phase 1) ───────────────
        # Shield is the first step in every canonical plan. One worker
        # loop per resource lane — both are CPU today (no GPU shield
        # steps), but we start both lanes so adding a future GPU
        # step (e.g. vision-based PII redaction) is a no-op.
        self._workflow_workers: list = []
        orchestrator_url = os.environ.get("NEXUS_ORCHESTRATOR_URL", "")
        if orchestrator_url:
            await self._start_canonical_workflow_workers(orchestrator_url)
        else:
            import logging as _logging
            _logging.getLogger("shield").info(
                "shield.workflow_workers_disabled "
                "reason=NEXUS_ORCHESTRATOR_URL_unset",
            )

    async def _start_canonical_workflow_workers(self, orchestrator_url: str) -> None:
        import asyncio
        import logging as _logging
        _log = _logging.getLogger("shield")
        from nexus_sdk.workflows import (
            StepKind, WorkerConfig, WorkflowWorker, queue_name,
        )
        from app.workflow_handlers import ShieldWorkflowHandlers

        token = os.environ.get("NEXUS_WORKER_TOKEN", "")
        handlers = ShieldWorkflowHandlers(self)

        # Shield is the cheapest engine; per-step wall time is milliseconds.
        # Run with much higher concurrency so a single shield pod can
        # service many gates per second.
        cpu_conc = int(os.environ.get("SHIELD_WORKER_CONCURRENCY_CPU", "16"))
        gpu_conc = int(os.environ.get("SHIELD_WORKER_CONCURRENCY_GPU", "1"))
        for kind in (StepKind.CPU, StepKind.GPU):
            lane = queue_name("shield", kind)
            q = self._build_workflow_lane_queue(lane)
            ok = await q.connect()
            if not ok:
                _log.error("shield.workflow_worker_redis_unreachable lane=%s", lane)
                continue
            worker = WorkflowWorker(
                config=WorkerConfig(
                    engine_name="shield",
                    kind=kind,
                    orchestrator_url=orchestrator_url,
                    auth_token=token,
                    concurrency=cpu_conc if kind == StepKind.CPU else gpu_conc,
                ),
                queue=q,
            )
            handlers.register(worker)
            self._workflow_workers.append(worker)
            asyncio.create_task(
                worker.run(), name=f"workflow_worker.{lane}",
            )
            _log.info(
                "shield.workflow_worker_started lane=%s orchestrator=%s",
                lane, orchestrator_url,
            )

    def _build_workflow_lane_queue(self, lane: str):
        from nexus_sdk.queue import JobQueue

        return JobQueue(
            engine_name=lane,
            redis_host=os.environ.get("REDIS_HOST", "redis"),
            redis_port=int(os.environ.get("REDIS_PORT", "6379")),
            redis_password=os.environ.get("REDIS_PASSWORD", ""),
            redis_db=int(os.environ.get("REDIS_DB", "3")),
        )

    async def _handle_transcription(self, event: NexusEvent):
        """Auto-redact incoming transcriptions."""
        text = event.data.get("transcript_text", "")
        if not text:
            return

        entities = self.detector.detect(text)
        safe_text, mapping_id, mapping = await self.redactor.redact(text, entities)

        await ShieldAuditLog.record(
            action="redact",
            tenant_id=event.tenant_id,
            user_id=None,
            mapping_id=mapping_id,
            entity_count=len(entities),
            entity_types=list(set(e["type"] for e in entities)),
        )

        if self.event_bus:
            await self.event_bus.publish(NexusEvent(
                event_type="shield.redaction.completed",
                tenant_id=event.tenant_id,
                trace_id=event.trace_id,
                engine="shield",
                session_id=event.session_id,
                data={
                    "safe_text": safe_text,
                    "mapping_id": mapping_id,
                    "entity_count": len(entities),
                    "original_session_id": event.session_id,
                },
            ))

    def register_routes(self, app):

        # ── Redact ────────────────────────────────────────────

        @app.post("/api/v1/shield/redact", response_model=RedactResponse)
        async def redact(
            req: RedactRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Detect and redact all PII from text."""
            start = time.monotonic()

            entities = self.detector.detect(req.text, req.custom_patterns)
            safe_text, mapping_id, mapping = await self.redactor.redact(
                req.text, entities
            )

            elapsed_ms = (time.monotonic() - start) * 1000

            await ShieldAuditLog.record(
                action="redact",
                tenant_id=req.tenant_id,
                user_id=user.user_id,
                mapping_id=mapping_id,
                entity_count=len(entities),
                entity_types=list(set(e["type"] for e in entities)),
            )

            return RedactResponse(
                success=True,
                trace_id=req.trace_id,
                engine="shield",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                safe_text=safe_text,
                mapping_id=mapping_id,
                entities_found=entities,
                entity_count=len(entities),
            )

        # ── Reveal (De-redact) ─────────────────────────────────

        @app.post("/api/v1/shield/reveal", response_model=RevealResponse)
        async def reveal(
            req: RevealRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Reverse a redaction (authorized users only)."""
            user.require_permission("shield.reveal")

            mapping = await self.redactor.reveal(req.mapping_id)
            if not mapping:
                raise HTTPException(status_code=404, detail="Mapping not found")

            original_parts = []
            for token, value in mapping.items():
                original_parts.append(f"{token} = {value}")

            await ShieldAuditLog.record(
                action="reveal",
                tenant_id=req.tenant_id,
                user_id=user.user_id,
                mapping_id=req.mapping_id,
                entity_count=len(mapping),
                entity_types=[],
            )

            return RevealResponse(original_text="\n".join(original_parts))

        # ── Analyze (detect without redacting) ─────────────────

        @app.post("/api/v1/shield/analyze", response_model=AnalyzeResponse)
        async def analyze(
            req: AnalyzeRequest,
            user: NexusUser = Depends(get_current_user),
        ):
            """Analyze text for PII without redacting."""
            start = time.monotonic()

            entities = self.detector.detect(req.text)
            elapsed_ms = (time.monotonic() - start) * 1000

            risk = "low"
            critical_types = {PIIType.SSN.value, PIIType.CREDIT_CARD.value}
            high_types = {PIIType.DATE_OF_BIRTH.value, PIIType.ACCOUNT_NUMBER.value}

            found_types = set(e["type"] for e in entities)
            if found_types & critical_types:
                risk = "critical"
            elif found_types & high_types:
                risk = "high"
            elif len(entities) > 5:
                risk = "medium"

            await ShieldAuditLog.record(
                action="detect",
                tenant_id=req.tenant_id,
                user_id=user.user_id,
                mapping_id=None,
                entity_count=len(entities),
                entity_types=list(found_types),
            )

            return AnalyzeResponse(
                success=True,
                trace_id=req.trace_id,
                engine="shield",
                engine_version="0.2.0",
                processing_time_ms=round(elapsed_ms, 2),
                entities=entities,
                risk_level=risk,
            )

        # ── Audit Log ──────────────────────────────────────────

        @app.get("/api/v1/shield/audit")
        async def get_audit_log(
            limit: int = 100,
            user: NexusUser = Depends(get_current_user),
        ):
            """Get Shield audit log for the current tenant."""
            return await ShieldAuditLog.get_log(
                tenant_id=user.tenant_id, limit=limit
            )


# ─── Entry Point ──────────────────────────────────────────────

def main():
    engine = ShieldEngine()
    engine.run()


if __name__ == "__main__":
    main()
