"""
Worker-side runtime for canonical workflows.

A worker pod registers one or more StepHandlers and runs a WorkflowWorker
loop. The loop:
  1. Pulls a JobEnvelope from its queue (CPU or GPU lane).
  2. Spawns a heartbeat task that bumps WorkflowManager.last_heartbeat
     every `envelope.heartbeat_seconds` so the orchestrator's
     stuck-workflow detector doesn't flag this job as orphaned.
  3. Awaits the handler. If the handler raises, builds a failure
     StepResult; if it returns, that becomes the StepResult.
  4. POSTs the result back to the orchestrator (HTTP) which calls
     WorkflowManager.record_result(). If the orchestrator is
     unreachable the worker NACKs the queue message so another worker
     (or this one after restart) picks it up.

The worker NEVER writes to workflow_state directly. The orchestrator's
record_result is the single transactional choke point that advances
state and persists checkpoints.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from nexus_sdk.workflows.models import JobEnvelope, StepKind, StepResult

logger = logging.getLogger(__name__)


StepHandler = Callable[[JobEnvelope], Awaitable[StepResult]]


@dataclass
class WorkerConfig:
    engine_name: str
    kind: StepKind                       # CPU or GPU lane
    orchestrator_url: str                # e.g. http://orchestrator:8100
    # The worker auth token used to call orchestrator endpoints. When
    # left empty the worker mints a service JWT from NEXUS_JWT_SECRET
    # on demand (see `_resolve_auth_token`) — production engine pods
    # always have NEXUS_JWT_SECRET so this falls back cleanly.
    auth_token: str = ""
    # The HTTP request timeout for callbacks. Independent of the per-
    # workflow / per-step deadline — those are enforced server-side.
    http_timeout_seconds: float = 30.0
    # In-pod handler concurrency. The worker holds at most `concurrency`
    # handlers in flight; pulling the next job blocks once the semaphore
    # is exhausted. For CPU lanes set 2-8 (lets one pod soak up multiple
    # workflows while waiting on I/O). For GPU lanes keep 1 — the
    # engine's own GPU semaphore will serialize anyway, and >1 here
    # just means more context-switching overhead.
    concurrency: int = 1


def _mint_worker_jwt(engine_name: str) -> str:
    """Mint a long-lived service JWT for worker→orchestrator callbacks.

    Workers call protected orchestrator endpoints
    (`/api/v1/canonical-workflows/.../result`,
    `/api/v1/canonical-workflows/.../heartbeat`). The orchestrator
    enforces JWT auth via `Depends(get_current_user)`. When the engine
    pod doesn't ship an explicit NEXUS_WORKER_TOKEN, mint one in-process
    using the shared NEXUS_JWT_SECRET so the canonical workflow plane
    runs out of the box.

    Returns "" if no secret is available (the worker will then hit 401
    and the queue layer will retry; the operator will see the warning
    in logs and can set NEXUS_WORKER_TOKEN explicitly).
    """
    import os
    secret = os.environ.get("NEXUS_JWT_SECRET", "") or os.environ.get("JWT_SECRET", "")
    if not secret:
        return ""
    try:
        import jwt
    except Exception:
        return ""
    now = int(time.time())
    return jwt.encode(
        {
            "sub": f"{engine_name}-workflow-worker",
            "email": f"{engine_name}-workflow-worker@internal",
            "tenant_id": "internal",
            "role": "admin",
            "permissions": ["*"],
            "iat": now,
            "exp": now + 86400 * 7,  # 7-day TTL; well past any pod restart cycle
        },
        secret,
        algorithm="HS256",
    )


class WorkflowWorker:
    """
    Long-running loop. Owns one JobQueue connection + one HTTP client.
    """

    def __init__(self, config: WorkerConfig, queue, manager_client: Optional[object] = None) -> None:
        self._cfg = config
        self._queue = queue
        # `manager_client` is an optional in-process WorkflowManager,
        # used in single-process dev runs. Production always uses HTTP
        # callbacks so workers can scale independently of the orchestrator.
        self._manager_client = manager_client
        self._handlers: dict[str, StepHandler] = {}
        self._running = False
        self._http: Optional[httpx.AsyncClient] = None
        # Resolve the auth token now. If the caller didn't pass one,
        # mint a service JWT from NEXUS_JWT_SECRET so callbacks to the
        # orchestrator authenticate cleanly out of the box.
        if not self._cfg.auth_token:
            minted = _mint_worker_jwt(self._cfg.engine_name)
            if minted:
                self._cfg.auth_token = minted
                logger.info(
                    "worker.minted_service_jwt engine=%s ttl_days=7",
                    self._cfg.engine_name,
                )
            else:
                logger.warning(
                    "worker.no_auth_token engine=%s — callbacks will 401",
                    self._cfg.engine_name,
                )

    # ─── Handler registration ────────────────────────────────────

    def register(self, step_name: str, handler: StepHandler) -> None:
        """Bind a step name to its handler. Caller's responsibility to
        register one handler per step the engine claims to support."""
        self._handlers[step_name] = handler

    # ─── Lifecycle ───────────────────────────────────────────────

    async def run(self) -> None:
        """
        Consume from the queue and dispatch each job to `_handle` with
        bounded in-pod concurrency.

        The previous implementation `await`-ed each handler before
        pulling the next job, which serialized one workflow at a time
        per pod regardless of model availability. This version uses a
        semaphore + create_task so a single pod can hold up to
        `WorkerConfig.concurrency` handlers in flight. Engine-level
        resource guards (e.g. PriorityGPUSemaphore in eyes) continue
        to serialize the actual GPU work; the worker-pool concurrency
        just lets CPU-bound prep work for job N+1 overlap with the
        GPU wait for job N.
        """
        self._running = True
        self._http = httpx.AsyncClient(timeout=self._cfg.http_timeout_seconds)
        concurrency = max(1, int(self._cfg.concurrency))
        sem = asyncio.Semaphore(concurrency)
        in_flight: set[asyncio.Task] = set()
        logger.info(
            "worker.started engine=%s kind=%s concurrency=%d handlers=%s",
            self._cfg.engine_name, self._cfg.kind.value, concurrency,
            sorted(self._handlers.keys()),
        )

        async def _run_one(job: dict) -> None:
            try:
                await self._handle(job)
            finally:
                sem.release()

        try:
            async for job in self._queue.consume():
                # Block here when the pool is saturated — backpressure
                # naturally propagates to the queue layer, which keeps
                # the unclaimed messages available for other pods.
                await sem.acquire()
                task = asyncio.create_task(_run_one(job))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        finally:
            # Drain in-flight work before tearing down the HTTP client.
            # gather(return_exceptions) so a single handler failure
            # doesn't suppress the others' completion.
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
            await self._http.aclose()
            self._http = None
            self._running = False

    def stop(self) -> None:
        self._running = False
        self._queue.stop()

    # ─── Per-job handling ───────────────────────────────────────

    async def _handle(self, job: dict) -> None:
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        msg_id = job.get("_msg_id") or job.get("msg_id")

        try:
            envelope = JobEnvelope.from_wire(payload)
        except Exception as e:
            logger.error("worker.bad_envelope err=%s payload_keys=%s",
                         e, sorted(payload.keys()))
            # Bad envelope — ack to drop; the orchestrator will catch
            # the missing workflow via its sweeper.
            await self._queue.ack(msg_id)
            return

        handler = self._handlers.get(envelope.step_name)
        if handler is None:
            logger.error(
                "worker.no_handler engine=%s step=%s",
                self._cfg.engine_name, envelope.step_name,
            )
            await self._report_result(StepResult(
                workflow_id=envelope.workflow_id,
                step_name=envelope.step_name,
                success=False,
                error=f"no handler for step {envelope.step_name!r}",
                fatal=True,
            ))
            await self._queue.ack(msg_id)
            return

        # If the envelope already expired, fail fast.
        if envelope.is_expired():
            await self._report_result(StepResult(
                workflow_id=envelope.workflow_id,
                step_name=envelope.step_name,
                success=False,
                error="envelope expired before handler ran",
            ))
            await self._queue.ack(msg_id)
            return

        # Run handler with a heartbeat sidekick.
        hb_task = asyncio.create_task(self._heartbeat_loop(envelope))
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                handler(envelope),
                timeout=envelope.seconds_remaining(),
            )
        except asyncio.TimeoutError:
            result = StepResult(
                workflow_id=envelope.workflow_id,
                step_name=envelope.step_name,
                success=False,
                error="step exceeded deadline",
                error_context={"attempt": envelope.attempt},
            )
        except Exception as e:
            result = StepResult(
                workflow_id=envelope.workflow_id,
                step_name=envelope.step_name,
                success=False,
                error=str(e),
                error_context={
                    "exception_type": type(e).__name__,
                    "attempt": envelope.attempt,
                },
            )
        finally:
            hb_task.cancel()
            try:
                await hb_task
            except (asyncio.CancelledError, Exception):
                pass
        result.duration_ms = int((time.monotonic() - start) * 1000)

        reported = await self._report_result(result)
        if reported:
            await self._queue.ack(msg_id)
        else:
            # Couldn't reach the orchestrator. Let the queue layer
            # reclaim and retry rather than ack now.
            await self._queue.nack(msg_id, job)

    async def _heartbeat_loop(self, envelope: JobEnvelope) -> None:
        interval = max(5, envelope.heartbeat_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send_heartbeat(envelope.workflow_id)
            except Exception as e:
                logger.debug("worker.heartbeat_failed wf=%s err=%s",
                             envelope.workflow_id, e)

    # ─── Callbacks to the orchestrator ───────────────────────────

    async def _report_result(self, result: StepResult) -> bool:
        # In-process shortcut for dev runs.
        if self._manager_client is not None:
            try:
                await self._manager_client.record_result(result)
                return True
            except Exception as e:
                logger.error("worker.local_report_failed wf=%s err=%s",
                             result.workflow_id, e)
                return False

        if self._http is None:
            return False
        url = f"{self._cfg.orchestrator_url.rstrip('/')}/api/v1/canonical-workflows/{result.workflow_id}/steps/{result.step_name}/result"
        headers = {"Content-Type": "application/json"}
        if self._cfg.auth_token:
            headers["Authorization"] = f"Bearer {self._cfg.auth_token}"
        try:
            resp = await self._http.post(
                url,
                json={
                    "success": result.success,
                    "checkpoint": result.checkpoint,
                    "error": result.error,
                    "error_context": result.error_context,
                    "duration_ms": result.duration_ms,
                    "fatal": result.fatal,
                },
                headers=headers,
            )
            if resp.status_code >= 500:
                logger.warning("worker.result_5xx wf=%s status=%d",
                               result.workflow_id, resp.status_code)
                return False
            return resp.status_code < 400
        except httpx.HTTPError as e:
            logger.warning("worker.result_http_error wf=%s err=%s",
                           result.workflow_id, e)
            return False

    async def _send_heartbeat(self, workflow_id: str) -> None:
        if self._manager_client is not None:
            await self._manager_client.heartbeat(workflow_id)
            return
        if self._http is None:
            return
        url = f"{self._cfg.orchestrator_url.rstrip('/')}/api/v1/canonical-workflows/{workflow_id}/heartbeat"
        headers = {}
        if self._cfg.auth_token:
            headers["Authorization"] = f"Bearer {self._cfg.auth_token}"
        await self._http.post(url, headers=headers)
