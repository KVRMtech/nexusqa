"""
QA Orchestrator — Full Pipeline Execution.

Background task that orchestrates: Spine → Shield → Ears → Eyes → Heart →
Hands → Backbone → Legs → Mouth → Nerves.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from nexus_sdk.auth import NexusUser, AuthService

from .config import OrchestratorConfig
from .models import PipelineStage, KTSession
from .store import RedisSessionStore

logger = logging.getLogger(__name__)


async def run_full_pipeline(
    session_id: str,
    tenant_id: str,
    user: NexusUser,
    sut_url: Optional[str],
    sut_credentials: Optional[dict],
    skip_execution: bool,
    notify: bool,
    *,
    store: RedisSessionStore,
    config: OrchestratorConfig,
    http_client: httpx.AsyncClient,
    auth: AuthService,
) -> None:
    """Background: execute the complete QA pipeline.

    State is persisted to Redis after every stage so the pipeline can resume.
    """
    session = await store.get_session(session_id)
    if not session:
        return
    sdata = await store.get_data(session_id)
    token = auth.create_token(user)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _add_timeline(event: str, detail: str):
        """Append a timeline event to the local sdata (saved with _save)."""
        sdata.setdefault("timeline", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "detail": detail,
        })

    async def _save():
        await store.save_session(session)
        await store.save_data(session_id, sdata)

    try:
        # ── Stage 1a: Wait for transcription ───────────────────
        _add_timeline("pipeline_started", "Full QA pipeline initiated")
        await _save()
        transcript_text = ""

        if session.audio_job_id and "transcription" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.TRANSCRIBING
            transcript_text = await _poll_ears_job(session.audio_job_id, headers, config=config, http_client=http_client)
            session.stages_completed.append("transcription")
            _add_timeline("transcription_done", "Transcription complete")
            await _save()

        # ── Stage 1b: Wait for visual analysis ────────────────
        visual_result = {}
        if session.video_job_id and "visual_analysis" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.VISUAL_ANALYZING
            await _save()
            visual_result = await _poll_eyes_job(session.video_job_id, headers, config=config, http_client=http_client)
            session.stages_completed.append("visual_analysis")
            sdata["visual_analysis"] = visual_result
            _add_timeline("visual_analysis_done", f"Visual analysis complete — {visual_result.get('total_frames_analyzed', 0)} frames analyzed")
            await _save()

        # ── Stage 2: Shield redaction ──────────────────────────
        if transcript_text and "shielding" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.SHIELDING
            shield_resp = await http_client.post(
                f"{config.shield_url}/api/v1/shield/redact",
                json={"tenant_id": tenant_id, "text": transcript_text},
                headers=headers,
            )
            shield_data = shield_resp.json()
            safe_text = shield_data.get("safe_text", transcript_text)
            pii_count = shield_data.get("entity_count", 0)
            session.stages_completed.append("shielding")
            _add_timeline("shield_done", f"PII redacted: {pii_count} entities removed")
            await _save()
        else:
            safe_text = ""

        # ── Stage 3: Extract business rules ────────────────────
        rules = sdata.get("rules", [])
        if "rule_extraction" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.EXTRACTING_RULES
            _add_timeline("extracting_rules", "Extracting business rules via Heart LLM")
            await _save()

            # Build extraction payload: transcript + visual context (option 2: audio primary, video supplemental)
            extraction_payload: dict = {
                "tenant_id": tenant_id,
                "transcript": safe_text,
                "session_id": session_id,
            }
            # Enrich with visual analysis if available
            has_content = bool(safe_text)
            if visual_result:
                visual_summary_parts = []
                for frame in visual_result.get("frames", []):
                    desc = frame.get("description") or frame.get("text") or ""
                    if desc:
                        visual_summary_parts.append(desc)
                visual_context = "\n".join(visual_summary_parts)
                if visual_context:
                    has_content = True
                    extraction_payload["visual_context"] = visual_context
                    # If no transcript, use visual context as the transcript input
                    if not safe_text:
                        extraction_payload["transcript"] = f"[Visual analysis - no audio transcript available]\n{visual_context}"

            if has_content:
                rules_resp = await http_client.post(
                    f"{config.heart_url}/api/v1/heart/extract-rules",
                    json=extraction_payload,
                    headers=headers,
                )
                rules_data = rules_resp.json()
                rules = rules_data.get("rules", [])
                edge_cases = rules_data.get("edge_cases", [])
                questions = rules_data.get("questions_for_sme", [])
            else:
                rules, edge_cases, questions = [], [], []
                _add_timeline("rules_skipped", "No transcript or visual content to extract rules from")

            session.rules_extracted = len(rules)
            sdata["rules"] = rules
            session.stages_completed.append("rule_extraction")
            _add_timeline("rules_extracted", f"{len(rules)} rules, {len(edge_cases)} edge cases, {len(questions)} SME questions")
            await _save()
        else:
            questions = []

        # ── Stage 4: Store in Backbone ─────────────────────────
        if "knowledge_storage" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.STORING_KNOWLEDGE
            for rule in rules:
                await http_client.post(
                    f"{config.backbone_url}/api/v1/backbone/rules",
                    json={"tenant_id": tenant_id, "rule": rule},
                    headers=headers,
                )
            session.stages_completed.append("knowledge_storage")
            _add_timeline("knowledge_stored", f"{len(rules)} rules stored in knowledge graph")
            await _save()

        # ── Stage 5: Generate test cases ───────────────────────
        test_cases = sdata.get("tests", [])
        if "test_generation" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.GENERATING_TESTS
            _add_timeline("generating_tests", "Generating test cases via Heart LLM")
            await _save()
            if rules:
                tests_resp = await http_client.post(
                    f"{config.heart_url}/api/v1/heart/generate-tests",
                    json={
                        "tenant_id": tenant_id,
                        "rules": rules,
                        "coverage_targets": ["happy_path", "boundary", "negative", "edge_case"],
                    },
                    headers=headers,
                )
                tests_data = tests_resp.json()
                test_cases = tests_data.get("test_cases", [])
            else:
                test_cases = []
                _add_timeline("tests_skipped", "No rules to generate tests from")
            session.tests_generated = len(test_cases)
            sdata["tests"] = test_cases
            session.stages_completed.append("test_generation")
            _add_timeline("tests_generated", f"{len(test_cases)} test cases generated")
            await _save()

        # ── Stage 5b: Generate synthetic test data ─────────────
        if "test_data_generation" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.GENERATING_TEST_DATA
            test_data_resp = await http_client.post(
                f"{config.hands_url}/api/v1/hands/generate-profiles",
                json={
                    "tenant_id": tenant_id,
                    "count": max(len(test_cases) * 2, 50),
                    "product_types": list({r.get("product_type", "term_20") for r in rules}),
                    "include_boundary_values": True,
                },
                headers=headers,
            )
            if test_data_resp.status_code == 200:
                test_data = test_data_resp.json()
                session.test_data_records = test_data.get("count", 0)
                sdata["test_data"] = test_data.get("profiles", [])
                session.stages_completed.append("test_data_generation")
                _add_timeline("test_data_generated", f"{session.test_data_records} synthetic test data records generated")
                await _save()

        # ── Stage 6: Execute tests ─────────────────────────────
        if not skip_execution and sut_url and test_cases and "test_execution" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.EXECUTING_TESTS
            total_passed = 0
            total_failed = 0

            for tc in test_cases:
                exec_resp = await http_client.post(
                    f"{config.legs_url}/api/v1/legs/execute",
                    json={
                        "tenant_id": tenant_id,
                        "test_case": tc,
                        "target_type": "web_ui",
                        "base_url": sut_url,
                        "credentials": sut_credentials,
                    },
                    headers=headers,
                )
                exec_data = exec_resp.json()
                job_id = exec_data.get("job_id")

                result = await _poll_legs_job(job_id, headers, config=config, http_client=http_client)
                sdata.setdefault("results", []).append(result)

                if result.get("status") == "passed":
                    total_passed += 1
                else:
                    total_failed += 1

            session.tests_executed = total_passed + total_failed
            session.tests_passed = total_passed
            session.tests_failed = total_failed
            session.stages_completed.append("test_execution")
            _add_timeline("tests_executed", f"Executed: {total_passed} passed, {total_failed} failed")
            await _save()

        # ── Stage 7: Generate reports via Mouth ────────────────
        if "report_generation" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.GENERATING_REPORTS
            try:
                report_resp = await http_client.post(
                    f"{config.mouth_url}/api/v1/mouth/generate",
                    json={
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "report_type": "full_session_report",
                        "format": "html",
                        "title": session.name,
                        "rules": rules,
                        "test_cases": test_cases,
                        "test_results": sdata.get("results", []),
                    },
                    headers=headers,
                )
                if report_resp.status_code == 200:
                    report_result = report_resp.json()
                    session.reports_generated += 1
                    sdata.setdefault("reports", []).append(report_result)
                    session.stages_completed.append("report_generation")
                    _add_timeline("reports_generated", f"Full session report generated: {report_result.get('report_id', 'N/A')}")
                    await _save()
            except Exception as exc:
                logger.error(
                    "pipeline.stage7.report_generation_failed",
                    extra={
                        "session_id": session_id,
                        "tenant_id": tenant_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                _add_timeline("report_warning", f"Report generation failed ({type(exc).__name__}: {exc}), continuing pipeline")
                await _save()

        # ── Stage 8: Notify ────────────────────────────────────
        if notify and "notification" not in session.stages_completed:
            session.pipeline_stage = PipelineStage.NOTIFYING
            summary = (
                f"Nexus QA Session '{session.name}' complete:\n"
                f"• {session.rules_extracted} rules extracted\n"
                f"• {session.tests_generated} tests generated\n"
                f"• {session.tests_passed}/{session.tests_executed} tests passed\n"
                f"• {len(questions)} questions for SME review"
            )
            try:
                await http_client.post(
                    f"{config.nerves_url}/api/v1/nerves/execute",
                    json={
                        "tenant_id": tenant_id,
                        "connector": "slack",
                        "action": "send_message",
                        "parameters": {"channel": "#nexus-qa", "text": summary},
                    },
                    headers=headers,
                )
            except Exception as exc:
                logger.error(
                    "pipeline.stage8.notification_failed",
                    extra={
                        "session_id": session_id,
                        "tenant_id": tenant_id,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                _add_timeline(
                    "notification_warning",
                    f"Notification failed ({type(exc).__name__}: {exc}), pipeline still completing",
                )

            session.stages_completed.append("notification")

        # ── Done ───────────────────────────────────────────────
        session.pipeline_stage = PipelineStage.COMPLETED
        _add_timeline("pipeline_completed", "Full QA pipeline completed successfully")
        await _save()

    except Exception as e:
        error_detail = str(e) or f"{type(e).__name__}: (no message)"
        logger.error("pipeline.failed", extra={"session_id": session_id, "error": error_detail, "error_type": type(e).__name__}, exc_info=True)
        session.pipeline_stage = PipelineStage.FAILED
        session.error = error_detail
        _add_timeline("pipeline_failed", f"Pipeline error: {error_detail}")
        await _save()


# ─── Polling Helpers ──────────────────────────────────────────

async def _poll_ears_job(
    job_id: str,
    headers: dict,
    *,
    config: OrchestratorConfig,
    http_client: httpx.AsyncClient,
    max_wait: int = 600,
) -> str:
    """Poll Ears engine for transcription completion."""
    import time
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        try:
            resp = await http_client.get(
                f"{config.ears_url}/api/v1/ears/jobs/{job_id}",
                headers=headers,
            )
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("pipeline.ears_poll.transport_error", extra={"error": str(exc) or type(exc).__name__, "job_id": job_id})
            await asyncio.sleep(10)
            continue
        job = resp.json()
        status = job.get("status", "")
        if status == "completed":
            result = job.get("result", {})
            segments = result.get("segments", [])
            return " ".join(s.get("text", "") for s in segments)
        elif status == "failed":
            raise Exception(f"Transcription failed: {job.get('error')}")
        await asyncio.sleep(5)
    raise Exception("Transcription timed out")


async def _poll_eyes_job(
    job_id: str,
    headers: dict,
    *,
    config: OrchestratorConfig,
    http_client: httpx.AsyncClient,
    max_wait: int = 900,
) -> dict:
    """Poll Eyes engine for video analysis completion."""
    import time
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        try:
            resp = await http_client.get(
                f"{config.eyes_url}/api/v1/eyes/jobs/{job_id}",
                headers=headers,
            )
        except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            logger.warning("pipeline.eyes_poll.transport_error", extra={"error": str(exc) or type(exc).__name__, "job_id": job_id})
            await asyncio.sleep(10)
            continue
        if resp.status_code != 200:
            logger.warning("pipeline.eyes_poll.error", extra={"status": resp.status_code, "body": resp.text[:200]})
            await asyncio.sleep(10)
            continue
        job = resp.json()
        status = job.get("status", "")
        if status == "completed":
            return job.get("result", job)
        elif status == "failed":
            raise Exception(f"Video analysis failed: {job.get('error')}")
        await asyncio.sleep(5)
    raise Exception("Video analysis timed out")


async def _poll_legs_job(
    job_id: str,
    headers: dict,
    *,
    config: OrchestratorConfig,
    http_client: httpx.AsyncClient,
    max_wait: int = 300,
) -> dict:
    """Poll Legs engine for test execution completion."""
    import time
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        resp = await http_client.get(
            f"{config.legs_url}/api/v1/legs/jobs/{job_id}",
            headers=headers,
        )
        job = resp.json()
        status = job.get("status", "")
        if status in ("passed", "failed", "error", "skipped"):
            return job.get("result", job)
        await asyncio.sleep(2)
    return {"status": "timeout", "job_id": job_id}


async def _log_timeline(
    store: RedisSessionStore,
    session_id: str,
    event: str,
    detail: str,
) -> None:
    """Log an event to the session timeline (persisted to Redis)."""
    sdata = await store.get_data(session_id)
    if sdata:
        sdata.setdefault("timeline", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "detail": detail,
        })
        await store.save_data(session_id, sdata)
