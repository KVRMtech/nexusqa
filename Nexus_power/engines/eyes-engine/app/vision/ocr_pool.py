"""
Process-isolated OCR pool.

The previous implementation wrapped `EasyOCR.extract_text` in
`asyncio.wait_for(asyncio.to_thread(...))`. That bounds the *await*
but leaves the underlying thread running indefinitely if EasyOCR
hangs — under load, hung threads accumulate in the executor and
every subsequent OCR call queues forever (architect P0 #2).

This module replaces it with a `ProcessPoolExecutor` that we can
hard-kill when a single OCR call exceeds its deadline:

  - One process per worker (default 1, env: EYES_OCR_MAX_WORKERS)
  - Each process bootstraps a long-lived OCREngine on first call
  - Timeout = kill the whole pool + recreate on next call
    (cheaper than per-call kill because EasyOCR cold-start is 3-5s)
  - Returns placeholder ("", [], 0.0) on timeout, never raises

Concurrency: a single `asyncio.Lock` serialises pool creation +
teardown but NOT extract() calls. Multiple concurrent extracts can
run if max_workers > 1, but a timeout on any one of them kills all
in-flight calls (they get placeholder results too — degraded but
non-blocking). This is the deliberate trade-off: prefer "lose a
batch" over "deadlock the engine forever".
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import multiprocessing as mp
import os
from typing import Optional

logger = logging.getLogger(__name__)


_PLACEHOLDER: tuple[str, list[dict], float] = ("", [], 0.0)


# Module-level singleton inside each child process. NOT shared with
# the parent — _ocr_singleton in the parent is always None.
_ocr_singleton = None


def _ocr_worker_init(
    languages: list[str],
    gpu: bool,
    model_dir: str,
    allow_remote_model_bootstrap: bool,
    load_timeout_seconds: float,
) -> None:
    """Run once per child process. Bootstraps OCREngine. Failures here
    abort the worker; the pool spawns a replacement on next submit.

    `OCREngine.load()` is `async def`, so a bare `eng.load()` call
    creates a coroutine and never awaits it — `RuntimeWarning:
    coroutine was never awaited` and OCR stays at the pre-load stub.
    The child process has no running event loop, so we spin up a
    fresh one for the single initialisation call and close it.
    """
    global _ocr_singleton
    import asyncio
    # Late import — keeps the parent process light. The child has its
    # own Python interpreter so import happens fresh.
    from app.vision import OCREngine
    eng = OCREngine(
        languages=languages,
        gpu=gpu,
        model_dir=model_dir,
        allow_remote_model_bootstrap=allow_remote_model_bootstrap,
        load_timeout_seconds=load_timeout_seconds,
    )
    try:
        asyncio.run(eng.load())
    except Exception:
        # Loading can fail in development without easyocr installed.
        # Let the OCREngine stay in stub mode; _ocr_worker_extract
        # will return the placeholder, which the orchestrator treats
        # as a non-fatal degraded OCR (same as a timeout).
        pass
    _ocr_singleton = eng


def _ocr_worker_extract(image_path: str) -> tuple[str, list[dict], float]:
    """Called inside a worker process for each frame."""
    if _ocr_singleton is None:
        # Initialiser failed; surface as placeholder so the parent
        # doesn't quarantine the workflow.
        return _PLACEHOLDER
    try:
        return _ocr_singleton.extract_text(image_path)
    except Exception:
        # Inside the child: log to stderr (the parent's logger is in
        # a different process). Return placeholder.
        import sys
        sys.stderr.write(f"ocr_worker.extract_failed path={image_path}\n")
        return _PLACEHOLDER


class OCRProcessPool:
    """Process-isolated OCR with hard timeout."""

    def __init__(
        self,
        max_workers: int = 1,
        frame_timeout_s: float = 60.0,
        languages: Optional[list[str]] = None,
        gpu: bool = False,
        model_dir: str = "./models/easyocr",
        allow_remote_model_bootstrap: bool = True,
        load_timeout_seconds: float = 30.0,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._frame_timeout_s = float(frame_timeout_s)
        self._languages = languages or ["en"]
        self._gpu = gpu
        self._model_dir = model_dir
        self._allow_remote_model_bootstrap = allow_remote_model_bootstrap
        self._load_timeout_seconds = load_timeout_seconds
        self._executor: Optional[concurrent.futures.ProcessPoolExecutor] = None
        self._lock = asyncio.Lock()
        # Counters for observability.
        self._timeouts = 0
        self._pool_recreates = 0

    # ─── Lifecycle ──────────────────────────────────────────────

    def _build_executor(self) -> concurrent.futures.ProcessPoolExecutor:
        ctx = mp.get_context("spawn")
        return concurrent.futures.ProcessPoolExecutor(
            max_workers=self._max_workers,
            mp_context=ctx,
            initializer=_ocr_worker_init,
            initargs=(
                self._languages,
                self._gpu,
                self._model_dir,
                self._allow_remote_model_bootstrap,
                self._load_timeout_seconds,
            ),
        )

    async def _ensure_pool(self) -> concurrent.futures.ProcessPoolExecutor:
        if self._executor is None:
            async with self._lock:
                if self._executor is None:
                    self._executor = self._build_executor()
                    self._pool_recreates += 1
                    logger.info(
                        "ocr_pool.created max_workers=%d timeout_s=%.1f recreate_count=%d",
                        self._max_workers, self._frame_timeout_s,
                        self._pool_recreates,
                    )
        return self._executor

    async def _kill_pool(self) -> None:
        """Tear down the worker pool with prejudice.

        `shutdown(wait=False, cancel_futures=True)` only cancels QUEUED
        futures; running tasks continue until they exit naturally. For
        a hung EasyOCR call that's exactly the failure mode we're
        trying to escape — the worker keeps running and the OS thread
        the SDK was waiting on is freed only when EasyOCR finally
        returns (potentially never). The architect's followup
        correctly flagged this.

        Fix: send SIGTERM to every child PID in the executor's private
        `_processes` map, then SIGKILL any survivors after a brief
        grace, then `shutdown(wait=False)`. New pool spawns fresh
        children on the next `_ensure_pool` call.
        """
        async with self._lock:
            stale = self._executor
            self._executor = None
        if stale is None:
            return

        # Snapshot the worker processes BEFORE shutdown — shutdown can
        # mutate the dict mid-iteration.
        workers = []
        try:
            workers = list((stale._processes or {}).values())
        except Exception:
            workers = []

        for proc in workers:
            try:
                if proc.is_alive():
                    proc.terminate()  # SIGTERM
            except Exception as e:
                logger.debug(
                    "ocr_pool.terminate_failed pid=%s err=%s",
                    getattr(proc, "pid", "?"), e,
                )

        # Brief grace window for SIGTERM to land. Don't await this on
        # the event loop — block via run_in_executor on the default
        # thread pool so we don't stall the loop.
        loop = asyncio.get_running_loop()

        def _join_all() -> list[int]:
            survivors: list[int] = []
            for proc in workers:
                try:
                    proc.join(timeout=2.0)
                    if proc.is_alive():
                        # SIGKILL — bypasses signal handlers and any
                        # native EasyOCR code holding the GIL.
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        proc.join(timeout=1.0)
                        if proc.is_alive():
                            survivors.append(int(getattr(proc, "pid", -1)))
                except Exception:
                    pass
            return survivors

        try:
            survivors = await loop.run_in_executor(None, _join_all)
        except Exception:
            survivors = []

        if survivors:
            logger.warning(
                "ocr_pool.kill_survivors count=%d pids=%s "
                "(workers ignored SIGTERM+SIGKILL — OS may still be "
                "tearing them down)",
                len(survivors), survivors,
            )

        try:
            # Now drop the executor handle. cancel_futures=True drops
            # the queued tasks we never started.
            stale.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            try:
                stale.shutdown(wait=False)
            except Exception:
                pass
        except Exception as e:
            logger.warning("ocr_pool.shutdown_failed err=%s", e)

    async def close(self) -> None:
        await self._kill_pool()

    # ─── API ────────────────────────────────────────────────────

    async def extract(
        self, image_path: str,
    ) -> tuple[str, list[dict], float]:
        """Run OCR on a single image. Returns ("", [], 0.0) on timeout
        or pool failure — never raises."""
        loop = asyncio.get_running_loop()
        try:
            executor = await self._ensure_pool()
            future = executor.submit(_ocr_worker_extract, image_path)
        except RuntimeError:
            # Pool was already shutting down between _ensure_pool and
            # submit. One retry with a fresh pool.
            await self._kill_pool()
            try:
                executor = await self._ensure_pool()
                future = executor.submit(_ocr_worker_extract, image_path)
            except Exception as e:
                logger.warning(
                    "ocr_pool.submit_failed_twice path=%s err=%s",
                    image_path, e,
                )
                return _PLACEHOLDER
        except Exception as e:
            logger.warning(
                "ocr_pool.submit_failed path=%s err=%s", image_path, e,
            )
            return _PLACEHOLDER

        try:
            result = await asyncio.wait_for(
                asyncio.wrap_future(future, loop=loop),
                timeout=self._frame_timeout_s,
            )
        except asyncio.TimeoutError:
            self._timeouts += 1
            logger.warning(
                "ocr_pool.timeout path=%s timeout_s=%.1f total_timeouts=%d",
                image_path, self._frame_timeout_s, self._timeouts,
            )
            # Hard-kill the pool — the worker is hung at the OS level
            # and won't free. cancel_futures=True drops pending tasks
            # so we don't waste effort on the rest of the batch.
            await self._kill_pool()
            return _PLACEHOLDER
        except concurrent.futures.CancelledError:
            # Pool was killed mid-flight (sibling timeout). Treat as
            # placeholder so the caller's loop continues.
            return _PLACEHOLDER
        except Exception as e:
            logger.warning(
                "ocr_pool.extract_error path=%s err=%s", image_path, e,
            )
            return _PLACEHOLDER

        # Normalise: pool serialisers can flatten tuple → list. Coerce
        # back to the canonical (str, list[dict], float) shape.
        try:
            text, regions, conf = result
        except (TypeError, ValueError):
            return _PLACEHOLDER
        return (str(text or ""), list(regions or []), float(conf or 0.0))

    # ─── Counters ───────────────────────────────────────────────

    @property
    def timeout_count(self) -> int:
        return self._timeouts

    @property
    def pool_recreates(self) -> int:
        return self._pool_recreates
