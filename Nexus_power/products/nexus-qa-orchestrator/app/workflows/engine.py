"""
Nexus Chain Engine — Executes workflow chains.

The brain of the orchestration layer.  Given a ChainDefinition and
input data it:
    1. Creates a WorkflowInstance
    2. Builds a DAG execution plan (topological sort with level grouping)
    3. Executes stages in dependency order (parallel where possible)
    4. Persists state to Redis after every stage (crash-safe resume)
    5. Returns the completed WorkflowInstance

Production features:
    - Exponential-backoff retries per stage
    - Async-job polling (Ears, Eyes, Legs)
    - for_each iteration with configurable concurrency
    - Multipart file upload support
    - Condition evaluation (skip stages dynamically)
    - on_failure control: fail / skip / continue
    - Output transforms between stages
    - Full timeline logging
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from nexus_sdk.config import production_guard
from nexus_sdk.observability.tracing import get_tracer

from .context import WorkflowContext
from .schema import (
    ChainDefinition,
    PollingConfig,
    StageDefinition,
    StageExecution,
    StageStatus,
    WorkflowInstance,
    WorkflowStatus,
)

logger = logging.getLogger(__name__)
_tracer = get_tracer("nexus.orchestrator.engine")


# ─── Engine URL Resolver ───────────────────────────────────────

class EngineURLResolver:
    """Maps engine names to their base URLs."""

    DEFAULT_URLS: dict[str, str] = {
        "shield": "http://localhost:8001",
        "ears": "http://localhost:8002",
        "eyes": "http://localhost:8003",
        "heart": "http://localhost:8004",
        "backbone": "http://localhost:8005",
        "nerves": "http://localhost:8006",
        "legs": "http://localhost:8007",
        "hands": "http://localhost:8008",
        "spine": "http://localhost:8009",
        "mouth": "http://localhost:8010",
        "brain": "http://localhost:8011",
    }

    def __init__(self, overrides: dict[str, str] | None = None):
        self._urls = dict(self.DEFAULT_URLS)
        if overrides:
            self._urls.update(overrides)

    def get_url(self, engine: str) -> str:
        url = self._urls.get(engine)
        if not url:
            raise ValueError(f"Unknown engine '{engine}'. Known: {list(self._urls)}")
        return url.rstrip("/")


# ─── Workflow Persistence Store ────────────────────────────────

class WorkflowStore:
    """
    Persists WorkflowInstance and WorkflowContext to Redis.
    Falls back to in-memory dicts when Redis is unavailable.
    """

    def __init__(self):
        self._redis = None
        self._mem_instances: dict[str, WorkflowInstance] = {}
        self._mem_contexts: dict[str, dict] = {}

    async def connect(self, redis_url: str):
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("WorkflowStore connected to Redis")
        except Exception as exc:
            logger.warning(
                "Redis unavailable for WorkflowStore — in-memory fallback: %s", exc
            )
            self._redis = None

        # Refuse in-memory fallback in production environments
        production_guard(
            "Redis (workflow-store)",
            available=self._redis is not None,
        )

    # ── Instance CRUD ──────────────────────────────────────────

    async def save_instance(self, instance: WorkflowInstance):
        payload = instance.model_dump_json()
        if self._redis:
            try:
                await self._redis.hset(
                    "chain:workflows", instance.workflow_id, payload
                )
                return
            except Exception as exc:
                logger.error("Redis save_instance failed: %s", exc)
        self._mem_instances[instance.workflow_id] = instance

    async def get_instance(self, workflow_id: str) -> Optional[WorkflowInstance]:
        if self._redis:
            try:
                raw = await self._redis.hget("chain:workflows", workflow_id)
                if raw:
                    return WorkflowInstance.model_validate_json(raw)
            except Exception as exc:
                logger.error("Redis get_instance failed: %s", exc)
        return self._mem_instances.get(workflow_id)

    async def list_instances(
        self, tenant_id: str, limit: int = 100, session_id: Optional[str] = None,
    ) -> list[WorkflowInstance]:
        instances = await self._all_instances()
        filtered = [i for i in instances if i.tenant_id == tenant_id]
        if session_id:
            filtered = [i for i in filtered if i.session_id == session_id]
        filtered.sort(key=lambda i: i.created_at, reverse=True)
        return filtered[:limit]

    async def _all_instances(self) -> list[WorkflowInstance]:
        if self._redis:
            try:
                raw_map = await self._redis.hgetall("chain:workflows")
                return [
                    WorkflowInstance.model_validate_json(v)
                    for v in raw_map.values()
                ]
            except Exception as exc:
                logger.error("Redis _all_instances failed: %s", exc)
        return list(self._mem_instances.values())

    # ── Context CRUD ───────────────────────────────────────────

    async def save_context(self, workflow_id: str, context_snapshot: dict):
        payload = json.dumps(context_snapshot, default=str)
        if self._redis:
            try:
                await self._redis.hset("chain:contexts", workflow_id, payload)
                return
            except Exception as exc:
                logger.error("Redis save_context failed: %s", exc)
        self._mem_contexts[workflow_id] = context_snapshot

    async def get_context(self, workflow_id: str) -> Optional[dict]:
        if self._redis:
            try:
                raw = await self._redis.hget("chain:contexts", workflow_id)
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                logger.error("Redis get_context failed: %s", exc)
        return self._mem_contexts.get(workflow_id)


# ─── File Store ────────────────────────────────────────────────

class FileStore:
    """
    Simple persistent file store for workflow uploads.
    Metadata cached in-memory and backed by Redis for recovery.
    """

    def __init__(self, base_path: str = "/data/nexus/orchestrator/uploads"):
        self._base = Path(base_path)
        self._base.mkdir(parents=True, exist_ok=True)
        self._redis = None
        self._meta: dict[str, dict] = {}

    async def connect(self, redis_url: str):
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
        except Exception:
            logger.warning("FileStore: Redis connection failed, using local-only storage", exc_info=True)
            self._redis = None

    async def recover(self):
        """Load file metadata from Redis on startup."""
        if not self._redis:
            return
        try:
            raw_map = await self._redis.hgetall("chain:files")
            for file_id, raw in raw_map.items():
                meta = json.loads(raw)
                if Path(meta["path"]).exists():
                    self._meta[file_id] = meta
            logger.info("Recovered %d file records from Redis", len(self._meta))
        except Exception as exc:
            logger.warning("File metadata recovery failed: %s", exc)

    async def store(
        self, filename: str, content: bytes, content_type: str, tenant_id: str
    ) -> dict:
        import uuid

        file_id = str(uuid.uuid4())
        tenant_dir = self._base / tenant_id
        tenant_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{file_id}_{filename}"
        file_path = tenant_dir / safe_name

        file_path.write_bytes(content)

        meta = {
            "file_id": file_id,
            "filename": filename,
            "path": str(file_path),
            "size_bytes": len(content),
            "content_type": content_type,
            "tenant_id": tenant_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._redis:
            try:
                await self._redis.hset("chain:files", file_id, json.dumps(meta))
            except Exception:
                logger.warning("FileStore: Failed to persist metadata to Redis for file %s", file_id, exc_info=True)
        self._meta[file_id] = meta
        return meta

    def get(self, file_id: str) -> Optional[dict]:
        return self._meta.get(file_id)

    async def read(self, file_id: str) -> Optional[bytes]:
        """Read the raw bytes of a stored file by its ID."""
        meta = self._meta.get(file_id)
        if not meta:
            return None
        file_path = Path(meta["path"])
        if not file_path.exists():
            return None
        return file_path.read_bytes()


# ─── Chain Engine ──────────────────────────────────────────────

class ChainEngine:
    """
    Executes workflow chains.

    Given a ChainDefinition and input data:
        1. Creates a WorkflowInstance
        2. Builds a leveled DAG execution plan
        3. Executes each level (parallel stages within a level)
        4. Persists state after every level (crash-safe)
        5. Supports full resume from last checkpoint
    """

    def __init__(
        self,
        url_resolver: EngineURLResolver,
        workflow_store: WorkflowStore,
        file_store: FileStore,
        http_client: httpx.AsyncClient,
        token_factory: Optional[Callable[[str], str]] = None,
    ):
        self.urls = url_resolver
        self.store = workflow_store
        self.files = file_store
        self.http = http_client
        self._token_factory = token_factory
        self._brain_enabled = self._check_brain_available()
        # Per-workflow locks to prevent stale overwrites during parallel stage persistence
        self._workflow_locks: dict[str, asyncio.Lock] = {}
        # Phase 1.6: Track dispatched job IDs per (workflow_id, stage_id) to prevent
        # duplicate engine submissions on retry/resume. Maps "wf:stage" → job_id.
        self._dispatched_jobs: dict[str, str] = {}

    @staticmethod
    def _compute_stall_thresholds(
        stage_def: StageDefinition,
        poll_cfg: PollingConfig,
    ) -> tuple[float, float]:
        """Derive stall thresholds from the stage's actual polling budget."""
        stage_budget = float(min(stage_def.timeout_seconds, poll_cfg.max_poll_seconds))
        degrade_secs = min(stage_budget, max(720.0, stage_budget * 0.75))
        warn_secs = min(
            max(poll_cfg.poll_interval_seconds, degrade_secs - poll_cfg.poll_interval_seconds),
            max(300.0, stage_budget * 0.25),
        )
        if warn_secs >= degrade_secs:
            warn_secs = max(poll_cfg.poll_interval_seconds, degrade_secs * 0.5)
        return round(warn_secs, 1), round(degrade_secs, 1)

    # ── Brain Integration ─────────────────────────────────────

    def _check_brain_available(self) -> bool:
        """Check if Brain engine URL is configured."""
        try:
            self.urls.get_url("brain")
            return True
        except ValueError:
            return False

    @property
    def _brain_policy_enforced(self) -> bool:
        """Whether the Brain acts as a hard policy gate (blocks workflow on fail).

        Controlled via BRAIN_POLICY_ENFORCE env var.
        Values: 'true'/'1' = hard gate, anything else = advisory only.
        """
        import os
        return os.getenv("BRAIN_POLICY_ENFORCE", "false").lower() in ("true", "1", "yes")

    async def _notify_brain_stage_complete(
        self,
        instance: WorkflowInstance,
        stage_def: StageDefinition,
        result: dict,
    ):
        """
        Notify Brain Engine that a pipeline stage completed.

        This enables:
        1. Session state tracking across engines
        2. Quality gate evaluation after key stages
        3. Gap analysis and next-action recommendations

        Non-blocking — failures are logged but never break the pipeline.
        """
        if not self._brain_enabled:
            return

        try:
            brain_url = self.urls.get_url("brain")
            headers = self._auth_headers(instance.tenant_id)

            # 1. Update session state in Brain
            await self.http.post(
                f"{brain_url}/api/v1/brain/sessions/{instance.session_id}/update",
                json={
                    "tenant_id": instance.tenant_id,
                    "session_id": instance.session_id,
                    "engine_name": stage_def.engine,
                    "result": result or {},
                },
                headers=headers,
                timeout=10.0,
            )
            logger.debug(
                "Brain notified: stage '%s' (engine=%s) for session %s",
                stage_def.stage_id, stage_def.engine, instance.session_id,
            )
        except Exception as exc:
            # Brain notification is best-effort — never block the pipeline
            logger.warning(
                "Brain notification failed for stage '%s': %s",
                stage_def.stage_id, exc,
            )

    async def request_brain_quality_gate(
        self,
        session_id: str,
        tenant_id: str,
        rules: list[dict] | None = None,
        test_cases: list[dict] | None = None,
        engine_results: dict | None = None,
        confidence_scores: dict[str, float] | None = None,
        pii_result: dict | None = None,
    ) -> dict | None:
        """
        Request a quality gate evaluation from the Brain Engine.

        Returns the quality score dict, or None if Brain is unavailable.
        This is called by the orchestrator at pipeline completion
        or can be called explicitly by the client via the Brain API.
        """
        if not self._brain_enabled:
            return None

        try:
            brain_url = self.urls.get_url("brain")
            headers = self._auth_headers(tenant_id)

            resp = await self.http.post(
                f"{brain_url}/api/v1/brain/quality-gate",
                json={
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "rules": rules or [],
                    "test_cases": test_cases or [],
                    "engine_results": engine_results or {},
                    "confidence_scores": confidence_scores or {},
                    "pii_result": pii_result,
                },
                headers=headers,
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Brain quality gate returned HTTP %s", resp.status_code)
        except Exception as exc:
            logger.warning("Brain quality gate request failed: %s", exc)
        return None

    # ── Quality-Gate Input Extraction ──────────────────────────

    def _extract_quality_gate_inputs(
        self,
        chain: ChainDefinition,
        instance: WorkflowInstance,
    ) -> dict[str, Any]:
        """
        Extract structured quality-gate inputs from completed stage outputs.

        P0 fix: Brain quality gate previously received only raw
        engine_results.  This method extracts rules, test_cases,
        confidence_scores, and pii_result from the appropriate stages
        so that the 5‑dimension scorer gets real data.
        """
        rules: list[dict] = []
        test_cases: list[dict] = []
        confidence_scores: dict[str, float] = {}
        pii_result: dict | None = None

        # Build stage_id → engine mapping from chain definition
        engine_by_stage: dict[str, str] = {
            sd.stage_id: sd.engine for sd in chain.stages
        }

        for stage_id, se in instance.stages.items():
            if not se.output or se.status != StageStatus.COMPLETED:
                continue
            output = se.output
            engine = engine_by_stage.get(stage_id, "")

            # Heart engine produces rules + test cases
            if engine == "heart":
                rules.extend(output.get("rules", []))
                test_cases.extend(output.get("test_cases", []))

            # Shield engine produces PII analysis result
            if engine == "shield":
                pii_result = {
                    "entity_count": output.get("entity_count", len(output.get("entities", []))),
                    "redacted": output.get("redacted", bool(output.get("safe_text"))),
                    "mapping_id": output.get("mapping_id", ""),
                    "entities": output.get("entities", []),
                }

            # Collect per-engine confidence scores
            conf = output.get("confidence")
            if conf is not None:
                confidence_scores[engine or stage_id] = float(conf)

        return {
            "rules": rules,
            "test_cases": test_cases,
            "confidence_scores": confidence_scores,
            "pii_result": pii_result,
        }

    async def request_brain_decision(
        self,
        session_id: str,
        tenant_id: str,
        decision_type: str,
        engine_results: dict | None = None,
        rules: list[dict] | None = None,
        test_cases: list[dict] | None = None,
        confidence_scores: dict[str, float] | None = None,
        user_query: str = "",
        constraints: dict | None = None,
    ) -> dict | None:
        """
        Ask the Brain Engine to make a routing/policy decision.

        P0: Enables Brain as a real governance gate — not just advisory.
        Returns the full decision dict, or None if Brain is unavailable.
        """
        if not self._brain_enabled:
            return None

        try:
            brain_url = self.urls.get_url("brain")
            headers = self._auth_headers(tenant_id)

            resp = await self.http.post(
                f"{brain_url}/api/v1/brain/decide",
                json={
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "decision_type": decision_type,
                    "engine_results": engine_results or {},
                    "rules": rules or [],
                    "test_cases": test_cases or [],
                    "confidence_scores": confidence_scores or {},
                    "user_query": user_query,
                    "constraints": constraints or {},
                },
                headers=headers,
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning("Brain decision returned HTTP %s", resp.status_code)
        except Exception as exc:
            logger.warning("Brain decision request failed: %s", exc)
        return None

    def _auth_headers(self, tenant_id: str) -> dict[str, str]:
        """Build auth headers for engine-to-engine calls."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token_factory:
            token = self._token_factory(tenant_id)
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ── Public API ─────────────────────────────────────────────

    async def start(
        self,
        chain: ChainDefinition,
        tenant_id: str,
        session_id: str,
        input_data: dict,
        created_by: str = "",
    ) -> WorkflowInstance:
        """Create a workflow instance. Returns immediately; call execute() in a background task."""
        instance = WorkflowInstance(
            chain_id=chain.chain_id,
            chain_name=chain.name,
            tenant_id=tenant_id,
            session_id=session_id,
            created_by=created_by,
            input_data=input_data,
            stages={
                stage.stage_id: StageExecution(stage_id=stage.stage_id)
                for stage in chain.stages
            },
        )
        self._timeline(
            instance,
            "workflow_created",
            f"Chain '{chain.name}' v{chain.version} workflow created",
        )

        ctx = WorkflowContext(
            instance.workflow_id,
            chain.chain_id,
            tenant_id,
            session_id,
            input_data,
        )
        await self._persist(instance, ctx)
        return instance

    async def execute(self, workflow_id: str, chain: ChainDefinition):
        """Execute the full workflow (call from a background task)."""
        instance = await self.store.get_instance(workflow_id)
        if not instance:
            logger.error("Workflow %s not found", workflow_id)
            return

        # Guard against re-execution of finished workflows
        if instance.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.CANCELLED,
        ):
            logger.warning(
                "Workflow %s already in terminal state '%s' — skipping execution",
                workflow_id,
                instance.status.value,
            )
            return

        with _tracer.start_as_current_span(
            f"workflow:{chain.chain_id}",
            attributes={
                "nexus.workflow_id": workflow_id,
                "nexus.chain_id": chain.chain_id,
                "nexus.tenant_id": instance.tenant_id,
            },
        ) as wf_span:
            await self._execute_workflow(
                workflow_id, chain, instance, wf_span,
            )

    async def _execute_workflow(
        self,
        workflow_id: str,
        chain: ChainDefinition,
        instance: WorkflowInstance,
        wf_span,
    ):
        """Inner workflow execution wrapped by OTel span."""

        # Restore context (enables resume after crash)
        ctx_snapshot = await self.store.get_context(workflow_id)
        if ctx_snapshot:
            ctx = WorkflowContext.from_snapshot(ctx_snapshot)
        else:
            ctx = WorkflowContext(
                instance.workflow_id,
                chain.chain_id,
                instance.tenant_id,
                instance.session_id,
                instance.input_data,
            )

        # Phase 1.6: Restore dispatched job IDs from context for crash-recovery dedup
        saved_jobs = ctx.data.get("_dispatched_jobs", {})
        for stage_id, job_id_val in saved_jobs.items():
            dispatch_key = f"{workflow_id}:{stage_id}"
            self._dispatched_jobs[dispatch_key] = job_id_val

        instance.status = WorkflowStatus.RUNNING
        instance.started_at = datetime.now(timezone.utc).isoformat()
        self._timeline(instance, "workflow_started", "Execution started")
        await self._persist(instance, ctx)

        try:
            plan = self._build_execution_plan(chain.stages)

            for level_stages in plan:
                # ── Check for cancellation before each level ──
                stored = await self.store.get_instance(workflow_id)
                if stored and stored.status == WorkflowStatus.CANCELLED:
                    instance.status = WorkflowStatus.CANCELLED
                    instance.completed_at = datetime.now(timezone.utc).isoformat()
                    self._timeline(
                        instance, "workflow_cancelled",
                        "Detected cancellation between levels",
                    )
                    break

                # Stages within a level run in parallel
                if len(level_stages) == 1:
                    await self._execute_stage(
                        level_stages[0], chain, instance, ctx
                    )
                else:
                    tasks = [
                        self._execute_stage(stage, chain, instance, ctx)
                        for stage in level_stages
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Surface any unhandled exceptions from parallel stages
                    for r in results:
                        if isinstance(r, Exception):
                            logger.error(
                                "Unhandled exception in parallel stage execution: %s", r,
                                exc_info=r,
                            )
                            if instance.status != WorkflowStatus.FAILED:
                                instance.status = WorkflowStatus.FAILED
                                instance.error = f"Unhandled parallel stage error: {r}"
                                instance.completed_at = datetime.now(timezone.utc).isoformat()

                await self._persist(instance, ctx)

                # Check if any stage caused a workflow-level failure
                if instance.status in (
                    WorkflowStatus.FAILED,
                    WorkflowStatus.CANCELLED,
                ):
                    break

            if instance.status not in (
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
            ):
                instance.status = WorkflowStatus.COMPLETED
                instance.completed_at = datetime.now(timezone.utc).isoformat()

                # ── Recovery pass ──────────────────────────────────────────
                # Retry any on_failure='skip' stages that were skipped but
                # whose dependencies are now all COMPLETED and whose condition
                # (if any) still evaluates to true.
                #
                # Primary case: persist_visual_evidence was dispatched before
                # artifact_persistence created the parent canonical_artifacts
                # row (stale execution plan or resume/retry timing), so it
                # received a 409 and was skipped.  By the time all planned
                # levels finish, artifact_persistence has completed — so we
                # can safely retry the evidence stage here.
                await self._recover_skipped_stages(chain, instance, ctx)
                await self._persist(instance, ctx)

                # Detect degraded completion: stages that skipped or failed
                # with on_failure=skip/continue leave gaps in the output.
                skipped_stages = [
                    sid for sid, se in instance.stages.items()
                    if se.status in (StageStatus.SKIPPED, StageStatus.FAILED)
                ]
                if skipped_stages:
                    instance.input_data["_degraded_stages"] = skipped_stages
                    self._timeline(
                        instance,
                        "workflow_degraded",
                        f"Stages not fully completed: {', '.join(skipped_stages)}",
                    )

                # ── Quality gate: single source of truth ─────────
                # For canonical pipelines, the canonical_quality_gate STAGE
                # is the authoritative final gate. We do NOT call the
                # standalone Brain QG (which scores QA-session quality and
                # can diverge from the media-quality canonical gate).
                # For non-canonical pipelines, the Brain QG is still used.
                quality_result: dict | None = None

                if chain.chain_id == "nexus.canonical-processing":
                    # Use canonical_quality_gate stage output as sole truth
                    cqg_stage = instance.stages.get("canonical_quality_gate")
                    if cqg_stage and cqg_stage.output:
                        quality_result = cqg_stage.output
                        # Strict: default to FAIL when 'passed' key is absent
                        gate_passed = quality_result.get("passed", False)
                        instance.input_data["_brain_quality_gate"] = {
                            "overall_score": quality_result.get("overall_score"),
                            "level": quality_result.get("level"),
                            "passed": gate_passed,
                            "gaps": quality_result.get("gaps", []),
                            "warnings": quality_result.get("warnings", []),
                            "review_reasons": quality_result.get("gaps", [])
                                + quality_result.get("warnings", []),
                        }
                        self._timeline(
                            instance,
                            "canonical_quality_gate_result",
                            f"Quality gate (canonical): {quality_result.get('level', 'unknown')} "
                            f"(score={quality_result.get('overall_score', 0):.2f}, "
                            f"passed={gate_passed})",
                        )
                    else:
                        # Stage missing/empty — strict: treat as failed,
                        # NOT as pass.  A missing quality gate means the
                        # artifact was never evaluated.
                        quality_result = {
                            "passed": False,
                            "overall_score": 0.0,
                            "level": "missing",
                            "gaps": ["canonical_quality_gate stage produced no output"],
                            "warnings": [],
                        }
                        gate_passed = False
                        instance.input_data["_brain_quality_gate"] = {
                            "overall_score": 0.0,
                            "level": "missing",
                            "passed": False,
                            "gaps": quality_result["gaps"],
                            "warnings": [],
                            "review_reasons": quality_result["gaps"],
                        }
                        self._timeline(
                            instance,
                            "canonical_quality_gate_result",
                            "Quality gate (canonical): stage output missing — "
                            "marking as FAILED (strict mode)",
                        )
                        logger.warning(
                            "Canonical quality gate produced no output for "
                            "workflow %s — marking needs_review",
                            workflow_id,
                        )
                else:
                    # Non-canonical: use standalone Brain QG
                    all_outputs = {
                        sid: se.output
                        for sid, se in instance.stages.items()
                        if se.output
                    }
                    extracted = self._extract_quality_gate_inputs(chain, instance)
                    quality_result = await self.request_brain_quality_gate(
                        session_id=instance.session_id,
                        tenant_id=instance.tenant_id,
                        rules=extracted["rules"],
                        test_cases=extracted["test_cases"],
                        engine_results=all_outputs,
                        confidence_scores=extracted["confidence_scores"],
                        pii_result=extracted["pii_result"],
                    )
                    if quality_result:
                        gate_passed = quality_result.get("passed", False)
                        review_reasons = (
                            quality_result.get("gaps", [])
                            + quality_result.get("warnings", [])
                        )
                        instance.input_data["_brain_quality_gate"] = {
                            "overall_score": quality_result.get("overall_score"),
                            "level": quality_result.get("level"),
                            "passed": gate_passed,
                            "gaps": quality_result.get("gaps", []),
                            "warnings": quality_result.get("warnings", []),
                            "review_reasons": review_reasons,
                        }
                        self._timeline(
                            instance,
                            "brain_quality_gate",
                            f"Quality gate: {quality_result.get('level', 'unknown')} "
                            f"(score={quality_result.get('overall_score', 0):.2f}, "
                            f"passed={gate_passed})",
                        )

                        # P0: Brain as a REAL policy gate — hold workflow on failure
                        if not gate_passed and self._brain_policy_enforced:
                            instance.status = WorkflowStatus.POLICY_BLOCKED
                            self._timeline(
                                instance,
                                "brain_policy_hold",
                                "Quality gate FAILED — workflow blocked by policy "
                                f"(score={quality_result.get('overall_score', 0):.2f}). "
                                "Set BRAIN_POLICY_ENFORCE=false to disable.",
                            )
                            logger.warning(
                                "Brain policy gate blocked workflow %s "
                                "(quality score %.2f, threshold not met)",
                                workflow_id,
                                quality_result.get("overall_score", 0),
                            )

                self._timeline(
                    instance,
                    "workflow_completed",
                    "All stages completed",
                )

                # Final status: if stages were skipped and quality gate
                # did not already demote, mark as degraded
                if (
                    instance.status == WorkflowStatus.COMPLETED
                    and instance.input_data.get("_degraded_stages")
                ):
                    instance.status = WorkflowStatus.DEGRADED
                    self._timeline(
                        instance,
                        "workflow_degraded",
                        "Workflow completed with degraded stages — non-fatal gaps present",
                    )

                # Phase 1.4: Write quality gate results back to canonical artifact
                if chain.chain_id == "nexus.canonical-processing":
                    await self._update_canonical_artifact_status(instance, quality_result)

                wf_span.set_attribute("nexus.workflow_status", instance.status.value)

        except Exception as exc:
            instance.status = WorkflowStatus.FAILED
            instance.error = str(exc)
            instance.completed_at = datetime.now(timezone.utc).isoformat()
            self._timeline(
                instance, "workflow_failed", f"Unhandled error: {exc}"
            )
            logger.exception("Workflow %s failed", workflow_id)
            wf_span.record_exception(exc)
            wf_span.set_attribute("nexus.workflow_status", "failed")

            # Phase 1.4: Mark artifact as failed when workflow fails
            if chain.chain_id == "nexus.canonical-processing":
                await self._mark_canonical_artifact_failed(instance, str(exc))

        # Final persist with write-through so platform API read model
        # always has the terminal workflow state (completed/failed/degraded).
        await self._persist_transition(instance, ctx)

        # Clean up per-workflow lock and dispatched job tracking
        self._workflow_locks.pop(workflow_id, None)
        # Remove all dispatched job entries for this workflow
        prefix = f"{workflow_id}:"
        keys_to_remove = [k for k in self._dispatched_jobs if k.startswith(prefix)]
        for k in keys_to_remove:
            self._dispatched_jobs.pop(k, None)

    async def cancel(self, workflow_id: str) -> bool:
        """Cancel a running workflow."""
        instance = await self.store.get_instance(workflow_id)
        if not instance:
            return False
        if instance.status not in (WorkflowStatus.CREATED, WorkflowStatus.RUNNING):
            return False
        instance.status = WorkflowStatus.CANCELLED
        instance.completed_at = datetime.now(timezone.utc).isoformat()
        self._timeline(instance, "workflow_cancelled", "Cancelled by user")
        await self.store.save_instance(instance)
        return True

    # ── Stage Execution ────────────────────────────────────────

    async def _recover_skipped_stages(
        self,
        chain: ChainDefinition,
        instance: WorkflowInstance,
        ctx: WorkflowContext,
    ) -> None:
        """Retry any on_failure='skip' stages that were skipped but are now unblocked.

        A stage is eligible for recovery when:
        1. Its status is SKIPPED (it ran and failed with on_failure='skip')
        2. All declared depends_on stages are now COMPLETED
        3. Its condition (if any) still evaluates to True

        This recovers the canonical case where persist_visual_evidence was
        dispatched before artifact_persistence had committed the parent row,
        received a 409/503, and was skipped — but by the time all planned
        levels finish, artifact_persistence has completed.
        """
        completed_ids = {
            sid for sid, se in instance.stages.items()
            if se.status == StageStatus.COMPLETED
        }

        for stage in chain.stages:
            se = instance.stages.get(stage.stage_id)
            if not se or se.status != StageStatus.SKIPPED:
                continue
            if stage.on_failure != "skip":
                # Condition-only skips (on_failure='fail') are intentional; skip them
                continue
            # All declared dependencies must be completed now
            if not all(dep in completed_ids for dep in (stage.depends_on or [])):
                continue
            # Condition must still evaluate to True (if present)
            if stage.condition and not ctx.evaluate_condition(stage.condition):
                continue

            # Reset stage state and re-execute
            se.status = StageStatus.PENDING
            se.error = None
            se.output = None
            se.started_at = None
            se.completed_at = None
            se.retries = 0
            self._timeline(
                instance,
                f"stage_recovery:{stage.stage_id}",
                f"Retrying skipped stage '{stage.name}' — all dependencies now satisfied",
            )
            logger.info(
                "Recovery: retrying skipped stage '%s' in workflow %s",
                stage.stage_id, instance.workflow_id,
            )
            await self._execute_stage(stage, chain, instance, ctx)

    async def _execute_stage(
        self,
        stage_def: StageDefinition,
        chain: ChainDefinition,
        instance: WorkflowInstance,
        ctx: WorkflowContext,
    ):
        """Execute a single stage with full error handling."""
        stage_exec = instance.stages[stage_def.stage_id]

        # Resume support: skip already-completed stages
        if stage_exec.status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
            return

        with _tracer.start_as_current_span(
            f"stage:{stage_def.stage_id}",
            attributes={
                "nexus.workflow_id": instance.workflow_id,
                "nexus.stage_id": stage_def.stage_id,
                "nexus.engine": stage_def.engine,
                "nexus.chain_id": chain.chain_id,
            },
        ) as span:
            await self._execute_stage_inner(
                stage_def, chain, instance, ctx, stage_exec, span
            )

    async def _execute_stage_inner(
        self,
        stage_def: StageDefinition,
        chain: ChainDefinition,
        instance: WorkflowInstance,
        ctx: WorkflowContext,
        stage_exec: StageExecution,
        span,
    ):
        """Inner stage execution wrapped by OTel span."""

        # Evaluate condition
        if stage_def.condition and not ctx.evaluate_condition(stage_def.condition):
            stage_exec.status = StageStatus.SKIPPED
            stage_exec.completed_at = datetime.now(timezone.utc).isoformat()
            ctx.set_stage_status(stage_def.stage_id, "skipped")
            self._timeline(
                instance,
                f"stage_skipped:{stage_def.stage_id}",
                f"Condition not met: {stage_def.condition}",
            )
            await self._persist_transition(instance, ctx)
            return

        stage_exec.status = StageStatus.RUNNING
        stage_exec.started_at = datetime.now(timezone.utc).isoformat()
        self._timeline(
            instance,
            f"stage_started:{stage_def.stage_id}",
            f"Starting: {stage_def.name}",
        )
        await self._persist_transition(instance, ctx)

        try:
            if stage_def.for_each:
                await self._execute_for_each(
                    stage_def, instance, ctx, stage_exec
                )
            else:
                result = await self._call_engine(
                    stage_def, ctx, workflow_id=instance.workflow_id,
                    instance=instance,
                )
                # Apply optional output transform
                if stage_def.output_transform:
                    result = self._apply_transform(
                        result, stage_def.output_transform
                    )
                stage_exec.output = result
                ctx.set_stage_output(stage_def.stage_id, result)
                ctx.set_stage_status(stage_def.stage_id, "completed")

            stage_exec.status = StageStatus.COMPLETED
            stage_exec.completed_at = datetime.now(timezone.utc).isoformat()
            stage_exec.duration_ms = self._calc_duration(stage_exec)
            span.set_attribute("nexus.stage_duration_ms", stage_exec.duration_ms)
            span.set_attribute("nexus.stage_status", "completed")
            self._timeline(
                instance,
                f"stage_completed:{stage_def.stage_id}",
                f"Completed: {stage_def.name} ({stage_exec.duration_ms:.0f}ms)",
            )
            await self._persist_transition(instance, ctx)

            # Notify Brain Engine of stage completion (best-effort, non-blocking)
            await self._notify_brain_stage_complete(
                instance, stage_def, stage_exec.output or {}
            )

        except Exception as exc:
            span.set_attribute("nexus.stage_status", "failed")
            span.record_exception(exc)
            stage_exec.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            stage_exec.completed_at = datetime.now(timezone.utc).isoformat()
            stage_exec.duration_ms = self._calc_duration(stage_exec)

            if stage_def.on_failure == "fail":
                stage_exec.status = StageStatus.FAILED
                instance.status = WorkflowStatus.FAILED
                instance.error = f"Stage '{stage_def.stage_id}' failed: {exc}"
                instance.completed_at = datetime.now(timezone.utc).isoformat()
                self._timeline(
                    instance,
                    f"stage_failed:{stage_def.stage_id}",
                    f"FATAL: {stage_def.name} — {exc}",
                )
            elif stage_def.on_failure == "skip":
                stage_exec.status = StageStatus.SKIPPED
                ctx.set_stage_status(stage_def.stage_id, "skipped")
                self._timeline(
                    instance,
                    f"stage_skipped:{stage_def.stage_id}",
                    f"Skipped on failure: {stage_def.name} — {exc}",
                )
            else:  # "continue"
                stage_exec.status = StageStatus.FAILED
                ctx.set_stage_status(stage_def.stage_id, "failed")
                ctx.set_stage_output(stage_def.stage_id, {})
                self._timeline(
                    instance,
                    f"stage_failed:{stage_def.stage_id}",
                    f"Failed (continuing): {stage_def.name} — {exc}",
                )
            await self._persist_transition(instance, ctx)

    # ── Engine HTTP Call ───────────────────────────────────────

    async def _call_engine(
        self,
        stage_def: StageDefinition,
        ctx: WorkflowContext,
        body_override: dict | None = None,
        workflow_id: str | None = None,
        instance: WorkflowInstance | None = None,
    ) -> Any:
        """
        Make an HTTP call to an engine with retries and optional polling.
        Supports JSON and multipart request types.

        Phase 1.6: For polling-enabled stages (ears/eyes), tracks dispatched
        job IDs so retries/resumes skip the POST and go straight to polling.
        """
        base_url = self.urls.get_url(stage_def.engine)
        logger.info(
            "Stage %s → engine=%s base_url=%s endpoint=%s",
            stage_def.stage_id, stage_def.engine, base_url, stage_def.endpoint,
        )
        body = (
            body_override
            if body_override is not None
            else ctx.resolve_mapping(stage_def.input_mapping)
        )

        # Resolve path-parameter templates (e.g. /artifacts/{session_id})
        endpoint = stage_def.endpoint
        if body and "{" in endpoint:
            try:
                endpoint = endpoint.format(
                    **{k: v for k, v in body.items() if v is not None}
                )
            except (KeyError, IndexError):
                pass  # Leave unresolved — will fail at HTTP level
        url = f"{base_url}{endpoint}"

        # Build headers
        headers: dict[str, str] = {}
        if self._token_factory:
            tenant_id = ctx.resolve("$workflow.tenant_id")
            token = self._token_factory(str(tenant_id))
            headers["Authorization"] = f"Bearer {token}"
        for hk, hv in stage_def.headers_mapping.items():
            resolved = ctx.resolve(hv) if isinstance(hv, str) and hv.startswith("$") else hv
            headers[hk] = str(resolved) if resolved is not None else ""

        # Phase 1.6: Check for previously dispatched job for polling stages
        dispatch_key = f"{workflow_id}:{stage_def.stage_id}" if workflow_id else None

        # Idempotency key base for polling-enabled engines (attempt appended in retry loop)
        _idem_key_base: Optional[str] = None
        if dispatch_key and stage_def.polling and stage_def.polling.enabled:
            _idem_key_base = hashlib.sha256(
                f"{workflow_id}:{stage_def.stage_id}:{json.dumps(body, sort_keys=True, default=str)}".encode()
            ).hexdigest()[:32]

        # Pre-dispatch readiness gate for async engines
        # Wait for the engine to be healthy before dispatching work,
        # so we don't send jobs into a restarting/loading container.
        if stage_def.polling and stage_def.polling.enabled:
            ready_url = f"{base_url}/health/ready"
            for ready_attempt in range(30):  # up to ~60s
                try:
                    r = await self.http.get(ready_url, timeout=5.0)
                    if r.status_code == 200:
                        break
                except Exception:
                    pass
                if ready_attempt == 29:
                    logger.warning(
                        "Stage %s: engine %s not ready after 60s, proceeding anyway",
                        stage_def.stage_id, stage_def.engine,
                    )
                await asyncio.sleep(2.0)

        # Retry loop
        retry = stage_def.retry_policy
        last_exc: Optional[Exception] = None

        for attempt in range(retry.max_retries + 1):
            # Set idempotency key with attempt number so retries create fresh jobs
            if _idem_key_base:
                headers["X-Idempotency-Key"] = f"{_idem_key_base}:{attempt}"
            try:
                # Phase 1.6: Reuse existing job on retry (skip POST)
                if (
                    dispatch_key
                    and stage_def.polling
                    and stage_def.polling.enabled
                    and dispatch_key in self._dispatched_jobs
                ):
                    existing_job_id = self._dispatched_jobs[dispatch_key]
                    logger.info(
                        "Stage %s attempt %d/%d: reusing existing job %s (dedup)",
                        stage_def.stage_id, attempt + 1,
                        retry.max_retries + 1, existing_job_id,
                    )
                    return await self._poll_for_result(
                        stage_def,
                        {stage_def.polling.job_id_path: existing_job_id},
                        headers,
                        instance=instance,
                        ctx=ctx,
                        token_factory=self._token_factory,
                        tenant_id=str(ctx.resolve("$workflow.tenant_id")) if ctx else None,
                    )

                response = await self._send_request(
                    stage_def, url, body, headers, ctx
                )

                if response.status_code >= 400:
                    if (
                        response.status_code in retry.retry_on_status
                        and attempt < retry.max_retries
                    ):
                        wait = retry.backoff_seconds * (
                            retry.backoff_multiplier ** attempt
                        )
                        logger.warning(
                            "Stage %s attempt %d/%d got HTTP %d, retrying in %.1fs",
                            stage_def.stage_id,
                            attempt + 1,
                            retry.max_retries + 1,
                            response.status_code,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue
                    response.raise_for_status()

                result = response.json()

                # Handle async polling
                if stage_def.polling and stage_def.polling.enabled:
                    # Phase 1.6: Store dispatched job_id before polling
                    job_id = self._extract_path(result, stage_def.polling.job_id_path)
                    if dispatch_key and job_id:
                        self._dispatched_jobs[dispatch_key] = str(job_id)
                        # Persist to context for crash-recovery
                        ctx.data.setdefault("_dispatched_jobs", {})[stage_def.stage_id] = str(job_id)

                    result = await self._poll_for_result(
                        stage_def, result, headers,
                        instance=instance,
                        ctx=ctx,
                        token_factory=self._token_factory,
                        tenant_id=str(ctx.resolve("$workflow.tenant_id")) if ctx else None,
                    )

                return result

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < retry.max_retries:
                    wait = retry.backoff_seconds * (
                        retry.backoff_multiplier ** attempt
                    )
                    logger.warning(
                        "Stage %s attempt %d/%d timed out, retrying in %.1fs",
                        stage_def.stage_id,
                        attempt + 1,
                        retry.max_retries + 1,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(
                    f"Stage '{stage_def.stage_id}' timed out after "
                    f"{retry.max_retries + 1} attempts"
                ) from exc

            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Engine '{stage_def.engine}' returned HTTP {exc.response.status_code}: "
                    f"{exc.response.text[:500]}"
                ) from exc

            except Exception as exc:
                last_exc = exc
                # If a dispatched job failed (e.g. engine restarted), clear the
                # dedup key so the next retry submits a fresh POST instead of
                # re-polling the same dead job.
                if dispatch_key and dispatch_key in self._dispatched_jobs:
                    logger.info(
                        "Stage %s: clearing dispatched job %s after failure, next retry will POST fresh",
                        stage_def.stage_id,
                        self._dispatched_jobs[dispatch_key],
                    )
                    del self._dispatched_jobs[dispatch_key]
                if attempt < retry.max_retries:
                    wait = retry.backoff_seconds * (
                        retry.backoff_multiplier ** attempt
                    )
                    logger.warning(
                        "Stage %s attempt %d/%d failed (%s: %s), retrying in %.1fs",
                        stage_def.stage_id,
                        attempt + 1,
                        retry.max_retries + 1,
                        type(exc).__name__,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)
                    # Wait for engine readiness before retrying (engine may have restarted)
                    if stage_def.polling and stage_def.polling.enabled:
                        ready_url = f"{base_url}/health/ready"
                        for ready_wait in range(30):
                            try:
                                rr = await self.http.get(ready_url, timeout=5.0)
                                if rr.status_code == 200:
                                    break
                            except Exception:
                                pass
                            await asyncio.sleep(2.0)
                    continue
                raise

        raise RuntimeError(
            f"Stage '{stage_def.stage_id}' exhausted {retry.max_retries + 1} attempts: {last_exc}"
        )

    async def _send_request(
        self,
        stage_def: StageDefinition,
        url: str,
        body: dict,
        headers: dict,
        ctx: WorkflowContext,
    ) -> httpx.Response:
        """Send the actual HTTP request (JSON, multipart, or query-param GET)."""

        if stage_def.request_type == "multipart":
            return await self._multipart_request(
                stage_def, url, body, headers, ctx
            )

        if stage_def.method.upper() in ("GET", "DELETE", "HEAD"):
            # Send input_mapping as query parameters
            params = {
                k: str(v)
                for k, v in body.items()
                if v is not None
            }
            return await self.http.request(
                method=stage_def.method,
                url=url,
                params=params,
                headers=headers,
                timeout=stage_def.timeout_seconds,
            )

        # POST / PUT / PATCH → JSON body
        headers["Content-Type"] = "application/json"
        return await self.http.request(
            method=stage_def.method,
            url=url,
            json=body,
            headers=headers,
            timeout=stage_def.timeout_seconds,
        )

    async def _multipart_request(
        self,
        stage_def: StageDefinition,
        url: str,
        body: dict,
        headers: dict,
        ctx: WorkflowContext,
    ) -> httpx.Response:
        """Handle multipart file upload requests."""
        files: dict[str, tuple[str, bytes, str]] = {}

        for field_name, file_ref in stage_def.file_mappings.items():
            file_id = (
                ctx.resolve(file_ref)
                if isinstance(file_ref, str) and file_ref.startswith("$")
                else file_ref
            )
            meta = self.files.get(str(file_id)) if file_id else None
            if meta:
                file_path = Path(meta["path"])
                if file_path.exists():
                    file_bytes = file_path.read_bytes()
                    files[field_name] = (
                        meta["filename"],
                        file_bytes,
                        meta.get("content_type", "application/octet-stream"),
                    )

        # Form data from input_mapping (strings only for multipart)
        data = {
            k: str(v)
            for k, v in body.items()
            if v is not None and not isinstance(v, (dict, list))
        }

        # Let httpx set Content-Type with boundary for multipart
        send_headers = {
            k: v for k, v in headers.items() if k.lower() != "content-type"
        }

        return await self.http.post(
            url,
            files=files,
            data=data,
            headers=send_headers,
            timeout=stage_def.timeout_seconds,
        )

    # ── Async Polling ──────────────────────────────────────────

    async def _poll_for_result(
        self,
        stage_def: StageDefinition,
        initial_response: dict,
        headers: dict,
        instance: WorkflowInstance | None = None,
        ctx: WorkflowContext | None = None,
        token_factory: Any | None = None,
        tenant_id: str | None = None,
    ) -> Any:
        """Poll an async engine job until completion or timeout.

        P1: Propagates engine job progress into stage execution state
        and detects stalls (same sub-stage for extended periods).
        """
        poll_cfg: PollingConfig = stage_def.polling  # type: ignore[assignment]

        job_id = self._extract_path(initial_response, poll_cfg.job_id_path)
        if not job_id:
            raise RuntimeError(
                f"No job ID found at path '{poll_cfg.job_id_path}' in response: "
                f"{json.dumps(initial_response)[:300]}"
            )

        base_url = self.urls.get_url(stage_def.engine)
        poll_url = f"{base_url}{poll_cfg.poll_endpoint.format(job_id=job_id)}"

        completion_lower = {s.lower() for s in poll_cfg.completion_statuses}
        failure_lower = {s.lower() for s in poll_cfg.failure_statuses}

        max_consecutive_errors = 60  # tolerate sustained CPU saturation (5 min @ 5s interval)
        consecutive_errors = 0

        # P1: Stall detection state
        _last_sub_stage: str = ""
        _sub_stage_since: float = time.monotonic()
        _STALL_WARN_SECS, _STALL_DEGRADE_SECS = self._compute_stall_thresholds(
            stage_def,
            poll_cfg,
        )
        _stall_warned = False

        # Resolve stage_exec for progress propagation
        stage_exec = (
            instance.stages.get(stage_def.stage_id)
            if instance else None
        )

        _TOKEN_REFRESH_SECS = 1800.0  # refresh JWT every 30 min
        _last_token_refresh = time.monotonic()

        start = time.monotonic()
        while time.monotonic() - start < poll_cfg.max_poll_seconds:
            await asyncio.sleep(poll_cfg.poll_interval_seconds)

            # Refresh auth token before it expires
            if (token_factory and tenant_id
                    and time.monotonic() - _last_token_refresh >= _TOKEN_REFRESH_SECS):
                headers["Authorization"] = f"Bearer {token_factory(tenant_id)}"
                _last_token_refresh = time.monotonic()

            try:
                resp = await self.http.get(
                    poll_url, headers=headers, timeout=30.0
                )
                data = resp.json()
                consecutive_errors = 0  # reset on success
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError) as exc:
                consecutive_errors += 1
                logger.warning(
                    "Polling %s — transient error #%d/%d (%s: %s), will retry",
                    stage_def.stage_id,
                    consecutive_errors,
                    max_consecutive_errors,
                    type(exc).__name__,
                    exc,
                )
                if consecutive_errors >= max_consecutive_errors:
                    raise RuntimeError(
                        f"Polling {stage_def.stage_id} aborted after "
                        f"{max_consecutive_errors} consecutive connection errors: {exc}"
                    ) from exc
                continue

            status_val = self._extract_path(data, poll_cfg.status_path)
            status_str = str(status_val).lower() if status_val else ""

            # P1: Extract and propagate engine progress into workflow state
            engine_progress = data.get("progress_percent", 0)
            engine_sub_stage = data.get("current_stage", status_str)
            elapsed_secs = time.monotonic() - start

            # Stall detection: track time spent in same sub-stage
            if engine_sub_stage != _last_sub_stage:
                _last_sub_stage = engine_sub_stage
                _sub_stage_since = time.monotonic()
                _stall_warned = False
            stall_secs = round(time.monotonic() - _sub_stage_since, 1)

            if stall_secs >= _STALL_WARN_SECS and not _stall_warned:
                _stall_warned = True
                logger.warning(
                    "P1-STALL: stage=%s engine=%s job=%s sub_stage='%s' "
                    "stalled for %.0fs (progress=%.1f%%)",
                    stage_def.stage_id, stage_def.engine, job_id,
                    engine_sub_stage, stall_secs, engine_progress,
                )
            if stall_secs >= _STALL_DEGRADE_SECS:
                logger.error(
                    "P1-STALL-DEGRADE: stage=%s engine=%s job=%s sub_stage='%s' "
                    "stalled for %.0fs — aborting stage",
                    stage_def.stage_id, stage_def.engine, job_id,
                    engine_sub_stage, stall_secs,
                )
                raise RuntimeError(
                    f"Stage '{stage_def.stage_id}' stalled for {stall_secs:.0f}s "
                    f"at sub_stage='{engine_sub_stage}' with no progress — aborting"
                )

            # Propagate progress into stage execution state
            if stage_exec:
                stage_exec.progress_detail = {
                    "progress_percent": engine_progress,
                    "current_stage": engine_sub_stage,
                    "engine_job_id": str(job_id),
                    "last_poll_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": round(elapsed_secs, 1),
                    "stall_seconds": stall_secs,
                }
                # Persist progress so GET /workflows/{id} reflects live state
                if instance and ctx:
                    try:
                        await self._persist_transition(instance, ctx)
                    except Exception:
                        pass  # best-effort progress persistence

            if status_str in completion_lower:
                result = self._extract_path(data, poll_cfg.result_path)
                return result if result is not None else data

            if status_str in failure_lower:
                error = data.get("error", "Unknown error")
                raise RuntimeError(
                    f"Async job {job_id} failed: {error}"
                )

            logger.info(
                "Polling %s — status=%s progress=%.1f%% sub_stage=%s (%.0fs elapsed, stall=%.0fs)",
                stage_def.stage_id,
                status_str,
                engine_progress,
                engine_sub_stage,
                elapsed_secs,
                stall_secs,
            )

        raise RuntimeError(
            f"Polling timed out after {poll_cfg.max_poll_seconds}s "
            f"for job {job_id} on stage '{stage_def.stage_id}'"
        )

    # ── for_each Execution ─────────────────────────────────────

    async def _execute_for_each(
        self,
        stage_def: StageDefinition,
        instance: WorkflowInstance,
        ctx: WorkflowContext,
        stage_exec: StageExecution,
    ):
        """Execute a stage once per item in a list."""
        items = ctx.resolve(stage_def.for_each)
        if not isinstance(items, (list, tuple)):
            if items is None:
                # Nothing to iterate — treat as empty
                stage_exec.iteration_results = []
                ctx.set_stage_output(
                    stage_def.stage_id, {"items": [], "count": 0}
                )
                ctx.set_stage_status(stage_def.stage_id, "completed")
                return
            raise RuntimeError(
                f"for_each path '{stage_def.for_each}' resolved to "
                f"{type(items).__name__}, expected list"
            )

        if not items:
            stage_exec.iteration_results = []
            ctx.set_stage_output(
                stage_def.stage_id, {"items": [], "count": 0}
            )
            ctx.set_stage_status(stage_def.stage_id, "completed")
            return

        semaphore = asyncio.Semaphore(stage_def.for_each_concurrency)

        async def _run_one(item: Any, index: int) -> Any:
            async with semaphore:
                # Create isolated context copy for concurrency safety
                local_ctx = ctx.with_temp(
                    {
                        stage_def.for_each_item_key: item,
                        "item_index": index,
                    }
                )
                body = local_ctx.resolve_mapping(stage_def.input_mapping)
                return await self._call_engine(
                    stage_def, ctx, body_override=body
                )

        if stage_def.for_each_concurrency == 1:
            # Sequential — deterministic order, easier to debug
            results = []
            for i, item in enumerate(items):
                try:
                    result = await _run_one(item, i)
                    results.append(result)
                except Exception as exc:
                    if stage_def.on_failure == "fail":
                        raise
                    results.append({"error": str(exc), "index": i})
        else:
            # Parallel with concurrency limit
            tasks = [_run_one(item, i) for i, item in enumerate(items)]
            raw_results = await asyncio.gather(
                *tasks, return_exceptions=True
            )
            results = []
            for i, r in enumerate(raw_results):
                if isinstance(r, Exception):
                    if stage_def.on_failure == "fail":
                        raise r
                    results.append({"error": str(r), "index": i})
                else:
                    results.append(r)

        stage_exec.iteration_results = results
        output = {"items": results, "count": len(results)}

        # Apply optional output transform (e.g. create derived structures)
        if stage_def.output_transform:
            output = self._apply_transform(output, stage_def.output_transform)

        ctx.set_stage_output(stage_def.stage_id, output)
        ctx.set_stage_status(stage_def.stage_id, "completed")
        ctx.clear_temp()

    # ── DAG Execution Plan ─────────────────────────────────────

    def _build_execution_plan(
        self, stages: list[StageDefinition]
    ) -> list[list[StageDefinition]]:
        """
        Build a leveled execution plan from stage dependencies.
        Uses Kahn's algorithm for topological sort with level grouping.

        Each level contains stages that can run in parallel.
        Stages in level N+1 depend only on stages in levels ≤ N.

        Raises ValueError on circular dependencies or unknown depends_on.
        """
        stage_map = {s.stage_id: s for s in stages}

        # Validate depends_on references
        for stage in stages:
            for dep in stage.depends_on:
                if dep not in stage_map:
                    raise ValueError(
                        f"Stage '{stage.stage_id}' depends on unknown "
                        f"stage '{dep}'"
                    )

        in_degree: dict[str, int] = {s.stage_id: 0 for s in stages}
        dependents: dict[str, list[str]] = defaultdict(list)

        for stage in stages:
            for dep in stage.depends_on:
                in_degree[stage.stage_id] += 1
                dependents[dep].append(stage.stage_id)

        # Kahn's algorithm with level grouping
        levels: list[list[StageDefinition]] = []
        queue = [sid for sid, deg in in_degree.items() if deg == 0]

        while queue:
            level = [stage_map[sid] for sid in queue]
            levels.append(level)

            next_queue: list[str] = []
            for sid in queue:
                for dependent_id in dependents.get(sid, []):
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        next_queue.append(dependent_id)
            queue = next_queue

        # Verify all stages were scheduled (detect cycles)
        scheduled = sum(len(level) for level in levels)
        if scheduled != len(stages):
            scheduled_ids = {
                s.stage_id for level in levels for s in level
            }
            missing = [
                s.stage_id for s in stages if s.stage_id not in scheduled_ids
            ]
            raise ValueError(
                f"Circular dependency detected involving stages: {missing}"
            )

        return levels

    # ── Output Transform ───────────────────────────────────────

    @staticmethod
    def _apply_transform(result: Any, transform: str) -> Any:
        """
        Apply a Python expression to transform a stage's output.
        The variable 'result' holds the raw response.
        """
        try:
            safe_globals: dict[str, Any] = {
                "__builtins__": {},
                "result": result,
                "len": len,
                "bool": bool,
                "int": int,
                "float": float,
                "str": str,
                "list": list,
                "dict": dict,
                "set": set,
                "sorted": sorted,
                "sum": sum,
                "min": min,
                "max": max,
                "any": any,
                "all": all,
                "enumerate": enumerate,
                "zip": zip,
                "range": range,
                "True": True,
                "False": False,
                "None": None,
            }
            return eval(transform, safe_globals)  # noqa: S307
        except Exception as exc:
            logger.warning(
                "Output transform failed: %s — returning raw result", exc
            )
            return result

    # ── Helpers ─────────────────────────────────────────────────

    async def _persist(
        self, instance: WorkflowInstance, ctx: WorkflowContext
    ):
        """Persist both instance and context after every stage."""
        await self.store.save_instance(instance)
        await self.store.save_context(instance.workflow_id, ctx.snapshot())

    async def _persist_transition(
        self, instance: WorkflowInstance, ctx: WorkflowContext
    ):
        """Persist after a stage status mutation with per-workflow locking.

        Phase 1.3: Called at every stage transition (running, skipped,
        completed, failed) so GET workflow is always truthful and
        crash-recovery never loses stage-level progress.

        P2: Write-through to Spine DB so platform API read model
        is always consistent with runtime state.

        Uses an asyncio.Lock per workflow_id so parallel stages within
        the same level don't produce stale-overwrite race conditions.
        """
        lock = self._workflow_locks.setdefault(
            instance.workflow_id, asyncio.Lock()
        )
        async with lock:
            await self._persist(instance, ctx)
            # P2: Write-through to Spine DB (best-effort, non-blocking)
            await self._write_through_spine(instance)

    async def _write_through_spine(self, instance: WorkflowInstance):
        """P2: Upsert workflow state to Spine DB for read-model consistency.

        Best-effort — failures are logged but never block the pipeline.
        Serialises the full WorkflowInstance into the workflow_instances
        table so platform API GET /workflows/{id} always returns fresh data.

        For terminal states (completed, failed, degraded, policy_blocked),
        retries up to 3 times with exponential backoff so the read-model
        is guaranteed to have the final workflow outcome.  For in-progress
        stage transitions the call remains best-effort (single attempt).
        """
        is_terminal = instance.status in (
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.DEGRADED,
            WorkflowStatus.POLICY_BLOCKED,
            WorkflowStatus.CANCELLED,
        )
        max_attempts = 4 if is_terminal else 1
        backoff_secs = [0, 1.0, 2.0, 4.0]

        try:
            spine_url = self.urls.get_url("spine")
            headers = self._auth_headers(instance.tenant_id)

            # Serialise stages as dicts for JSON column
            stages_dict = {}
            for sid, se in instance.stages.items():
                stages_dict[sid] = se.model_dump(mode="json")

            payload = {
                "workflow_id": instance.workflow_id,
                "chain_id": instance.chain_id,
                "chain_name": instance.chain_name,
                "tenant_id": instance.tenant_id,
                "session_id": instance.session_id,
                "created_by": instance.created_by,
                "status": instance.status.value,
                "input_data": instance.input_data,
                "stages": stages_dict,
                "timeline": instance.timeline[-20:],  # last 20 for size
                "error": instance.error,
                "started_at": instance.started_at,
                "completed_at": instance.completed_at,
            }

            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                if attempt > 0:
                    await asyncio.sleep(backoff_secs[attempt])
                    headers = self._auth_headers(instance.tenant_id)
                try:
                    resp = await self.http.post(
                        f"{spine_url}/api/v1/spine/persist-workflow",
                        json=payload,
                        headers=headers,
                        timeout=10.0 if is_terminal else 5.0,
                    )
                    body = resp.json()
                    if resp.status_code == 200 and body.get("success"):
                        if attempt > 0:
                            logger.info(
                                "P2 write-through succeeded for workflow %s on attempt %d",
                                instance.workflow_id, attempt + 1,
                            )
                        return  # success
                    last_exc = RuntimeError(
                        f"status={resp.status_code} body={resp.text[:200]}"
                    )
                except Exception as exc:
                    last_exc = exc

                if is_terminal and attempt < max_attempts - 1:
                    logger.warning(
                        "P2 write-through attempt %d/%d failed for terminal workflow %s: %s",
                        attempt + 1, max_attempts, instance.workflow_id, last_exc,
                    )

            # All attempts exhausted
            if is_terminal:
                logger.error(
                    "P2 write-through FAILED after %d attempts for terminal workflow %s "
                    "(status=%s). Platform read-model may be stale. Last error: %s",
                    max_attempts, instance.workflow_id, instance.status.value, last_exc,
                )
            else:
                logger.debug(
                    "P2 write-through failed for workflow %s: %s",
                    instance.workflow_id, last_exc,
                )
        except Exception as exc:
            if is_terminal:
                logger.error(
                    "P2 write-through to Spine failed for terminal workflow %s: %s",
                    instance.workflow_id, exc,
                )
            else:
                logger.debug(
                    "P2 write-through to Spine failed (non-blocking): %s", exc,
                )

    async def _update_canonical_artifact_status(
        self,
        instance: WorkflowInstance,
        quality_result: dict | None,
    ):
        """Write quality gate results back to canonical artifact in Spine.

        Phase 1.4: Artifact status is the official completion signal.
        After the workflow completes and quality gate runs, push the
        gate outcome to the canonical artifact row so downstream
        consumers can read artifact_id as the authoritative readiness.
        """
        artifact_stage = instance.stages.get("artifact_persistence")
        if not artifact_stage or not artifact_stage.output:
            return
        artifact_id = artifact_stage.output.get("artifact_id")
        if not artifact_id:
            return

        try:
            spine_url = self.urls.get_url("spine")
            headers = self._auth_headers(instance.tenant_id)

            payload: dict = {}
            if quality_result:
                passed = quality_result.get("passed", False)
                outcome = "pass" if passed else (
                    "needs_review" if not passed and self._brain_policy_enforced
                    else "fail"
                )
                review_reasons = quality_result.get("gaps", []) + quality_result.get("warnings", [])
                payload = {
                    "brain_quality_score": quality_result.get("overall_score"),
                    "quality_gate_passed": passed,
                    "quality_gate_outcome": outcome,
                    "review_reasons": review_reasons,
                }
            else:
                # No quality result available — strict: mark as needs_review,
                # NOT pass.  An unevaluated artifact must not be treated as
                # passing quality.
                payload = {
                    "brain_quality_score": None,
                    "quality_gate_passed": False,
                    "quality_gate_outcome": "needs_review",
                    "review_reasons": [
                        "quality_gate_missing: no quality evaluation was performed",
                    ],
                }

            # Append degraded-stage reasons if present
            degraded = instance.input_data.get("_degraded_stages", [])
            if degraded:
                existing = payload.get("review_reasons", [])
                existing.extend(
                    [f"stage_degraded:{s}" for s in degraded]
                )
                payload["review_reasons"] = existing

            await self.http.post(
                f"{spine_url}/api/v1/spine/artifacts/{artifact_id}/quality-gate",
                json=payload,
                headers=headers,
                timeout=10.0,
            )

            # Transition artifact from persisted → completed/needs_review
            # A failed or needs_review quality gate must never produce a 'completed' artifact
            qg_outcome = payload.get("quality_gate_outcome")
            if qg_outcome == "pass":
                final_status = "completed"
            else:
                final_status = "needs_review"
            await self.http.post(
                f"{spine_url}/api/v1/spine/artifacts/{artifact_id}/status",
                json={"status": final_status},
                headers=headers,
                timeout=10.0,
            )

            logger.info(
                "Artifact %s quality gate updated: outcome=%s passed=%s status=%s",
                artifact_id,
                payload.get("quality_gate_outcome"),
                payload.get("quality_gate_passed"),
                final_status,
            )

            # Update session status in platform-api so the UI reflects completion
            await self._update_session_status(instance.session_id, final_status, headers)

        except Exception as exc:
            logger.warning("Failed to update artifact quality gate: %s", exc)

    async def _mark_canonical_artifact_failed(
        self,
        instance: WorkflowInstance,
        error: str,
    ):
        """Mark canonical artifact as failed when workflow fails.

        Phase 1.4: Workflow failure → artifact failure mapping.
        """
        artifact_stage = instance.stages.get("artifact_persistence")
        artifact_id = None
        if artifact_stage and artifact_stage.output:
            artifact_id = artifact_stage.output.get("artifact_id")

        if not artifact_id:
            return

        try:
            spine_url = self.urls.get_url("spine")
            headers = self._auth_headers(instance.tenant_id)
            await self.http.post(
                f"{spine_url}/api/v1/spine/artifacts/{artifact_id}/status",
                json={"status": "failed", "error": error[:1000]},
                headers=headers,
                timeout=10.0,
            )
            logger.info("Artifact %s marked failed: %s", artifact_id, error[:200])
        except Exception as exc:
            logger.warning("Failed to mark artifact failed: %s", exc)

    async def _update_session_status(
        self,
        session_id: str,
        status: str,
        headers: dict,
    ):
        """Update session status in platform-api after processing completes."""
        import os
        platform_api_url = os.environ.get("PLATFORM_API_URL", "")
        if not platform_api_url or not session_id:
            return
        try:
            await self.http.patch(
                f"{platform_api_url}/api/v1/sessions/{session_id}",
                json={"status": status},
                headers=headers,
                timeout=5.0,
            )
            logger.info("Session %s status updated to %s", session_id, status)
        except Exception as exc:
            logger.warning("Failed to update session status: %s", exc)

    @staticmethod
    def _timeline(instance: WorkflowInstance, event: str, detail: str):
        instance.timeline.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "detail": detail,
            }
        )

    @staticmethod
    def _extract_path(data: Any, path: str) -> Any:
        """Extract a value from nested dict/list using dot-separated path."""
        current = data
        for key in path.split("."):
            if isinstance(current, dict):
                current = current.get(key)
            elif isinstance(current, (list, tuple)):
                try:
                    current = current[int(key)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        return current

    @staticmethod
    def _calc_duration(stage_exec: StageExecution) -> float:
        if stage_exec.started_at and stage_exec.completed_at:
            try:
                start = datetime.fromisoformat(stage_exec.started_at)
                end = datetime.fromisoformat(stage_exec.completed_at)
                return (end - start).total_seconds() * 1000
            except Exception:
                logger.debug("Failed to parse stage execution timestamps", exc_info=True)
        return 0.0
