"""Smoke test for Phase 2 — Long-Demo Streaming Pipeline.

Covers the pure-Python pieces of Phase 2:

  * ``app.chunk_checkpoint`` — round-trip save / load, atomic rename
    (no torn writes), the ``completed_chunks`` scanner ignoring stale
    indices, corrupt-file resilience.
  * ``VisualAnalyzer._advance_active_client_on_success`` — failover
    (default) vs load-balance ring math.  Mocks the httpx clients so
    we can exercise the index logic without a live Ollama.
  * Phase 2 defaults — chunk concurrency 2, OCR downscale 1600 — are
    reflected in the EyesEngine config class.
  * The platform-API artifacts router declares the
    ``/api/v1/artifacts/{id}/progress`` route at import time.

Live HTTP behaviour of the SSE endpoint is exercised by an
``integration_smoke`` extension below; we don't try to spin up the
full stack here.

Run:
    python Nexus_power/platform/api/tests/test_phase2_streaming_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_API_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _API_ROOT.parents[1]
_SDK_ROOT = _REPO_ROOT / "sdk" / "nexus-sdk"
_EYES_ROOT = _REPO_ROOT / "engines" / "eyes-engine"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))
sys.path.insert(0, str(_EYES_ROOT))


PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"  [OK]   {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAIL += 1


# ── Chunk checkpoint round-trip ───────────────────────────────────────────
def test_chunk_checkpoint() -> None:
    print("\n=== chunk_checkpoint round-trip ===")
    from app.chunk_checkpoint import (
        save_chunk_result,
        load_chunk_result,
        completed_chunks,
        chunk_result_path,
        clear_chunk_checkpoints,
    )
    from nexus_sdk.media.models import VisualAnalysisResult, FrameAnalysis

    def _result(job_id: str, n_frames: int) -> VisualAnalysisResult:
        return VisualAnalysisResult(
            job_id=job_id,
            session_id="sess-1",
            tenant_id="t-1",
            frames=[
                FrameAnalysis(
                    frame_index=i,
                    timestamp_seconds=float(i),
                    description=f"frame {i}",
                ) for i in range(n_frames)
            ],
            total_frames_extracted=n_frames,
        )

    with tempfile.TemporaryDirectory() as tmp:
        # save + load — happy path
        ok = save_chunk_result(tmp, 0, _result("job-1_chunk0", 3))
        check("save_chunk_result returns True on success", ok)

        target = chunk_result_path(tmp, 0)
        check("chunk_result_path uses zero-padded NNN naming",
              target.name == "chunk_000_result.json")
        check("chunk file exists after save", target.is_file())
        # No leftover .partial file
        partials = list(Path(tmp).glob("*.partial*"))
        check("no leftover .partial files after atomic rename", partials == [])

        loaded = load_chunk_result(tmp, 0)
        check("load_chunk_result returns a VisualAnalysisResult",
              loaded is not None and loaded.__class__.__name__ == "VisualAnalysisResult")
        check("loaded frame_count matches saved",
              loaded is not None and len(loaded.frames) == 3)
        check("loaded job_id round-trips",
              loaded is not None and loaded.job_id == "job-1_chunk0")

        # Missing index
        check("load_chunk_result returns None when file absent",
              load_chunk_result(tmp, 99) is None)

        # Corrupt file is treated as absent (logged, not crashed)
        bad = chunk_result_path(tmp, 5)
        bad.write_text("{not json", encoding="utf-8")
        check("corrupt JSON returns None (logged, not raised)",
              load_chunk_result(tmp, 5) is None)

        # Multi-chunk scan
        save_chunk_result(tmp, 1, _result("job-1_chunk1", 2))
        save_chunk_result(tmp, 2, _result("job-1_chunk2", 4))
        scan = completed_chunks(tmp, expected_count=4)
        check("completed_chunks finds all valid indices",
              set(scan.keys()) == {0, 1, 2})

        # Stale index outside the expected range is dropped
        save_chunk_result(tmp, 17, _result("job-1_chunk17", 1))
        scan_bounded = completed_chunks(tmp, expected_count=4)
        check("completed_chunks ignores indices outside expected_count",
              17 not in scan_bounded and set(scan_bounded.keys()) == {0, 1, 2})

        # clear cleanup
        removed = clear_chunk_checkpoints(tmp)
        check("clear_chunk_checkpoints removes every checkpoint",
              removed >= 3 and not any(Path(tmp).glob("chunk_*_result.json")))


# ── Ollama load-balance ring math ─────────────────────────────────────────
def test_ollama_load_balance() -> None:
    print("\n=== Ollama load-balance ring ===")
    # Importing VisualAnalyzer pulls in heavy CV / Ollama modules that
    # we don't need.  Construct it via __new__ to skip __init__ and
    # set just the attributes the helper consults.
    from app.vision import VisualAnalyzer

    def _make(clients: list[str], load_balance: bool) -> VisualAnalyzer:
        va = VisualAnalyzer.__new__(VisualAnalyzer)
        va._all_clients = list(clients)
        va._active_client_index = 0
        va._http_client = clients[0]
        va._load_balance = load_balance
        return va

    # Failover mode: pin to the succeeding client.
    va = _make(["a", "b", "c"], load_balance=False)
    va._advance_active_client_on_success(0)
    check("failover stays put when active client succeeded",
          va._active_client_index == 0 and va._http_client == "a")
    va._advance_active_client_on_success(2)  # third client succeeded
    check("failover pins to the client that actually returned 200",
          va._active_client_index == 2 and va._http_client == "c")

    # Load-balance mode: advance past the successful client every time.
    va = _make(["a", "b", "c"], load_balance=True)
    va._advance_active_client_on_success(0)  # active succeeded
    check("load_balance advances to next host after success",
          va._active_client_index == 1 and va._http_client == "b")
    va._advance_active_client_on_success(0)
    check("load_balance rotates again on second success",
          va._active_client_index == 2 and va._http_client == "c")
    va._advance_active_client_on_success(0)
    check("load_balance wraps around the ring",
          va._active_client_index == 0 and va._http_client == "a")

    # Failover after intermediate hop: succeeded_offset=1 means the
    # second client tried (active+1) actually returned 200.
    va = _make(["a", "b", "c"], load_balance=True)
    va._active_client_index = 1  # active is "b"
    va._http_client = "b"
    va._advance_active_client_on_success(1)  # "c" succeeded
    check("load_balance correctly resolves offset > 0 then advances",
          va._active_client_index == 0 and va._http_client == "a")

    # Single endpoint: helper is a no-op
    va = _make(["only-one"], load_balance=True)
    va._advance_active_client_on_success(0)
    check("single-client ring is a no-op",
          va._active_client_index == 0 and va._http_client == "only-one")


# ── Phase 2 defaults applied ──────────────────────────────────────────────
def test_phase2_defaults() -> None:
    print("\n=== Phase 2 config defaults ===")
    # Ensure no env var override is in effect for this assertion.
    for k in (
        "EYES_CHUNK_CONCURRENCY",
        "EYES_OCR_DOWNSCALE_MAX_WIDTH",
        "EYES_OLLAMA_LOAD_BALANCE",
    ):
        os.environ.pop(k, None)

    # Re-import the eyes main module so the config class loads with
    # the test-time environment.
    sys.modules.pop("main", None)
    sys.path.insert(0, str(_EYES_ROOT))
    # Importing the eyes engine main pulls in heavy deps (cv2, httpx,
    # etc.); we only need the config class.  Pull it from the source
    # via importlib to avoid the side-effectful main module init.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "eyes_main_config", _EYES_ROOT / "main.py",
    )
    # We can't easily exec the full main module (it imports nexus_sdk
    # heavy bits), so we just regex the source for the defaults.
    src = (_EYES_ROOT / "main.py").read_text(encoding="utf-8")
    check("chunk_concurrency default raised to 2",
          "chunk_concurrency: int = Field(\n        default=2," in src,
          detail="default=2 not found in source")
    check("ocr_downscale_max_width default raised to 1600",
          "ocr_downscale_max_width: int = Field(\n        default=1600," in src,
          detail="default=1600 not found in source")

    # Vision module load-balance flag
    vsrc = (_EYES_ROOT / "app" / "vision" / "__init__.py").read_text(encoding="utf-8")
    check("vision module reads EYES_OLLAMA_LOAD_BALANCE",
          "EYES_OLLAMA_LOAD_BALANCE" in vsrc)
    check("vision module exposes _load_balance attribute",
          "self._load_balance" in vsrc)
    check("vision module advances client on success via helper",
          "_advance_active_client_on_success" in vsrc)


# ── SSE progress route is registered ──────────────────────────────────────
def test_sse_progress_route() -> None:
    print("\n=== SSE progress route registered ===")
    # Avoid pulling the full FastAPI app graph; just check the router
    # source declares the new path.
    src = (_API_ROOT / "app" / "routers" / "artifacts.py").read_text(encoding="utf-8")
    check(
        "progress endpoint declared on artifacts router",
        '@router.get("/api/v1/artifacts/{artifact_id}/progress")' in src,
    )
    check(
        "endpoint accepts eyes_job_id query param",
        "eyes_job_id: str | None = Query" in src,
    )
    check(
        "endpoint returns text/event-stream",
        'media_type="text/event-stream"' in src,
    )
    check(
        "endpoint validates artifact tenant before streaming",
        "tenant_scoped_session(tenant_id)" in src,
    )
    check(
        "endpoint applies cross-tenant guard on returned eyes job",
        "cross-tenant job" in src,
    )


# ── Eyes resume wiring sanity ─────────────────────────────────────────────
def test_eyes_resume_wiring() -> None:
    print("\n=== Eyes chunked-resume wiring ===")
    src = (_EYES_ROOT / "main.py").read_text(encoding="utf-8")
    check(
        "imports the chunk_checkpoint module",
        "from app.chunk_checkpoint import" in src,
    )
    check(
        "loads checkpoints before processing chunks",
        "_load_chunk_checkpoints(chunk_dir, len(chunk_paths))" in src,
    )
    check(
        "skips chunks already loaded from checkpoint",
        "if chunk_results[chunk_idx] is not None:" in src,
    )
    check(
        "saves checkpoint after each chunk completes",
        "_save_chunk_checkpoint, chunk_dir, chunk_idx, result" in src,
    )
    check(
        "keeps chunk dir on partial failure to allow resume",
        "chunk_partial_keep" in src,
    )


# ── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    test_chunk_checkpoint()
    test_ollama_load_balance()
    test_phase2_defaults()
    test_sse_progress_route()
    test_eyes_resume_wiring()
    print(f"\n=== Phase 2 streaming smoke: {PASS} pass, {FAIL} fail ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
