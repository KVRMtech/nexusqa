"""Disk-based per-chunk checkpoint store for long-video processing.

Why disk instead of a Postgres ``processing_checkpoints`` table:

  * The chunk directory already lives on the same persistent volume as
    the source video (``{video_path.parent}/{job_id}_chunks``), so a
    pod restart on a long demo resumes from exactly the right place
    without an extra service round-trip.
  * Each chunk result is bounded (~1-2 MB JSON for a 5-minute chunk at
    2 fps).  Writing once, reading once on resume.  No DB transaction
    overhead per chunk.
  * Cleanup is automatic: the chunk directory is removed at the end of
    a successful run, taking checkpoints with it.

A chunk's result file is named ``chunk_NNN_result.json`` (zero-padded,
matching the ffmpeg segment naming convention) and contains a
JSON-serialised :class:`VisualAnalysisResult`.  We also write
``chunk_NNN_result.partial`` first and rename atomically so a crash
mid-write leaves no half-written checkpoint to confuse the next run.

The module is intentionally side-effect-light: any file-system error
when writing a checkpoint is logged but does NOT fail the chunk —
worst case the job re-processes that chunk on resume.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import structlog

from nexus_sdk.media.models import VisualAnalysisResult


logger = structlog.get_logger()


_CHUNK_RESULT_PATTERN = "chunk_{idx:03d}_result.json"


def chunk_result_path(chunk_dir: str | os.PathLike, chunk_index: int) -> Path:
    """Return the canonical result-file path for a chunk index."""
    return Path(chunk_dir) / _CHUNK_RESULT_PATTERN.format(idx=chunk_index)


def save_chunk_result(
    chunk_dir: str | os.PathLike,
    chunk_index: int,
    result: VisualAnalysisResult,
) -> bool:
    """Persist ``result`` for ``chunk_index`` under ``chunk_dir``.

    Returns True on success.  All failures are caught and logged —
    callers continue regardless because re-processing a chunk on
    resume is always safe, just slower.
    """
    target = chunk_result_path(chunk_dir, chunk_index)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # pydantic v2 -> model_dump(mode="json") emits JSON-native types
        # (datetimes as ISO strings, enums as values).  Write to a temp
        # file in the same directory then rename — same-FS rename is
        # atomic, so a torn write never produces a half-valid JSON.
        payload = result.model_dump(mode="json")
        fd, tmp_path = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".partial",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_path, target)
        except Exception:
            # Best-effort cleanup of the orphaned temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.info(
            "eyes.chunk_checkpoint_saved",
            chunk_dir=str(chunk_dir),
            chunk_index=chunk_index,
            frame_count=len(result.frames),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.warning(
            "eyes.chunk_checkpoint_save_failed",
            chunk_dir=str(chunk_dir),
            chunk_index=chunk_index,
            error=str(exc)[:200],
        )
        return False


def load_chunk_result(
    chunk_dir: str | os.PathLike,
    chunk_index: int,
) -> Optional[VisualAnalysisResult]:
    """Load a previously-saved chunk result, or ``None`` if absent / unreadable.

    A corrupt file is treated as absent — the caller will re-process
    the chunk rather than crashing.  We log so an operator can spot a
    persistent failure across runs.
    """
    src = chunk_result_path(chunk_dir, chunk_index)
    if not src.is_file():
        return None
    try:
        with src.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return VisualAnalysisResult.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — non-fatal
        logger.warning(
            "eyes.chunk_checkpoint_load_failed",
            chunk_dir=str(chunk_dir),
            chunk_index=chunk_index,
            error=str(exc)[:200],
        )
        return None


def completed_chunks(
    chunk_dir: str | os.PathLike,
    expected_count: int,
) -> dict[int, VisualAnalysisResult]:
    """Scan ``chunk_dir`` for all valid chunk-result files.

    Returns a mapping of ``chunk_index -> VisualAnalysisResult`` for
    every chunk that has a readable result file.  Indices outside
    ``[0, expected_count)`` are ignored so a stale checkpoint left
    over from a previous run with different chunk count is dropped.
    """
    found: dict[int, VisualAnalysisResult] = {}
    chunk_path = Path(chunk_dir)
    if not chunk_path.is_dir():
        return found
    for entry in chunk_path.glob("chunk_*_result.json"):
        try:
            # The filename is exactly "chunk_NNN_result.json"; extract NNN.
            stem = entry.stem  # "chunk_NNN_result"
            parts = stem.split("_")
            if len(parts) < 3 or parts[0] != "chunk" or parts[-1] != "result":
                continue
            idx = int(parts[1])
        except (ValueError, IndexError):
            continue
        if idx < 0 or idx >= expected_count:
            continue
        loaded = load_chunk_result(chunk_dir, idx)
        if loaded is not None:
            found[idx] = loaded
    return found


def clear_chunk_checkpoints(chunk_dir: str | os.PathLike) -> int:
    """Remove every checkpoint file under ``chunk_dir``.

    Returns the count of files deleted.  Used at the end of a
    successful run to free disk before the broader ``shutil.rmtree``
    in the eyes-engine cleanup path.
    """
    count = 0
    chunk_path = Path(chunk_dir)
    if not chunk_path.is_dir():
        return 0
    for entry in chunk_path.glob("chunk_*_result.json"):
        try:
            entry.unlink()
            count += 1
        except OSError:
            pass
    return count
