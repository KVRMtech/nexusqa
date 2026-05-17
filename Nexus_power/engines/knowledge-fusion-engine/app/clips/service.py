"""Clip service — ffmpeg cuts, idempotent cache, signed URLs.

Flow on ``request_clip``:

    1. Compute the canonical (tenant_id, session_id, kind, start_ms, end_ms)
       window. The unique constraint on ``media_clips`` makes lookups
       deterministic.
    2. Read existing ``media_clips`` row; if present, bump ``hits``
       and return a fresh signed URL.
    3. Resolve the source media URI for the artifact via the supplied
       ``MediaResolver``. URIs supported: local file path, ``file://``,
       ``http(s)://``, ``s3://``.
    4. Stream the cut window with ffmpeg into a temp file. Optionally
       generate a JPG thumbnail at the midpoint.
    5. Upload to S3 with SSE-KMS (or SSE-S3 fallback).
    6. INSERT the row; if a concurrent worker already wrote it, fall
       back to reading that row and reusing its keys.
    7. Return the signed URL.

ffmpeg is invoked as a subprocess with strict time and output-size
limits so a malformed input can't exhaust the host.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..canonical_reader import CanonicalReader
from ..db import Database, media_clips
from .s3 import S3ClipStorage

logger = logging.getLogger(__name__)


# ── Exceptions ──────────────────────────────────────────────────


class ClipError(Exception):
    """Base error for clip-service failures."""


class ClipNotResolvable(ClipError):
    """The source media URI could not be resolved for the artifact."""


class ClipExtractionFailed(ClipError):
    """ffmpeg returned a non-zero exit code or no output."""


# ── DTOs ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClipRequest:
    tenant_id: str
    session_id: str
    artifact_id: Optional[str]
    segment_id: Optional[str]
    kind: str  # 'video' | 'audio'
    start_ms: int
    end_ms: int
    pad_before_ms: int = 5000
    pad_after_ms: int = 5000

    def normalised_window(self) -> tuple[int, int]:
        start = max(0, self.start_ms - self.pad_before_ms)
        end = max(start + 1, self.end_ms + self.pad_after_ms)
        return start, end


@dataclass(frozen=True)
class Clip:
    clip_id: str
    s3_bucket: str
    s3_key: str
    content_type: str
    duration_ms: int
    size_bytes: Optional[int]
    thumbnail_key: Optional[str]
    signed_url: str
    thumbnail_signed_url: Optional[str]
    cached: bool


# ── Source-media resolver ───────────────────────────────────────


class MediaResolver:
    """Abstract: maps an artifact to a fetchable source URI."""

    async def resolve(
        self, *, tenant_id: str, session_id: str, artifact_id: Optional[str]
    ) -> str:  # pragma: no cover — interface only
        raise NotImplementedError


_FULL_PATHS = (
    ("source_media_uri",),
    ("media_uri",),
    ("media", "uri"),
    ("audio", "uri"),
    ("video", "uri"),
    ("ears", "audio_uri"),
    ("eyes", "video_uri"),
    ("source", "uri"),
)


class CanonicalArtifactResolver(MediaResolver):
    """Default resolver: read ``full_artifact_json`` for a URI key.

    Recognised keys (first non-empty wins): see ``_FULL_PATHS``.
    """

    def __init__(self, reader: CanonicalReader):
        self._reader = reader

    async def resolve(
        self, *, tenant_id: str, session_id: str, artifact_id: Optional[str]
    ) -> str:
        if not artifact_id:
            raise ClipNotResolvable(
                "artifact_id required to resolve source media"
            )
        artifact = await self._reader.fetch_artifact(
            tenant_id=tenant_id, artifact_id=artifact_id
        )
        if artifact is None:
            raise ClipNotResolvable(
                f"canonical_artifact {artifact_id} not found for tenant"
            )

        # Direct columns first.
        direct = artifact.get("source_filename")
        if isinstance(direct, str) and direct:
            return direct

        full = artifact.get("full_artifact_json") or {}
        for path in _FULL_PATHS:
            node: Any = full
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, str) and node.strip():
                return node.strip()

        raise ClipNotResolvable(
            f"no source URI in canonical_artifact {artifact_id} for tenant"
        )


# ── ClipService ─────────────────────────────────────────────────


class ClipService:
    DEFAULT_VIDEO_CONTAINER = "mp4"
    DEFAULT_AUDIO_CONTAINER = "m4a"

    def __init__(
        self,
        *,
        db: Database,
        storage: S3ClipStorage,
        resolver: MediaResolver,
        ffmpeg_path: str = "ffmpeg",
        max_wall_clock_seconds: int = 90,
        max_output_bytes: int = 500 * 1024 * 1024,
        thumbnail_enabled: bool = True,
    ) -> None:
        self._db = db
        self._storage = storage
        self._resolver = resolver
        self._ffmpeg = ffmpeg_path
        self._max_wall = max_wall_clock_seconds
        self._max_bytes = max_output_bytes
        self._thumbnail = thumbnail_enabled

    # ── Public API ──────────────────────────────────────────────

    async def request_clip(self, req: ClipRequest) -> Clip:
        if req.kind not in ("video", "audio"):
            raise ClipError(f"unsupported kind: {req.kind}")
        start_ms, end_ms = req.normalised_window()
        if end_ms <= start_ms:
            raise ClipError("end_ms must be greater than start_ms")

        cached = await self._lookup_cached(
            req.tenant_id, req.session_id, req.kind, start_ms, end_ms
        )
        if cached is not None:
            await self._bump_hits(req.tenant_id, cached["clip_id"])
            signed = await self._storage.presigned_url(s3_key=cached["s3_key"])
            thumb_signed = (
                await self._storage.presigned_url(
                    s3_key=cached["thumbnail_key"]
                )
                if cached["thumbnail_key"]
                else None
            )
            return Clip(
                clip_id=cached["clip_id"],
                s3_bucket=cached["s3_bucket"],
                s3_key=cached["s3_key"],
                content_type=cached["content_type"],
                duration_ms=cached["duration_ms"],
                size_bytes=cached["size_bytes"],
                thumbnail_key=cached["thumbnail_key"],
                signed_url=signed,
                thumbnail_signed_url=thumb_signed,
                cached=True,
            )

        # Resolve source media URI.
        source_uri = await self._resolver.resolve(
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            artifact_id=req.artifact_id,
        )

        clip_id = uuid.uuid4().hex
        extension = (
            self.DEFAULT_VIDEO_CONTAINER
            if req.kind == "video"
            else self.DEFAULT_AUDIO_CONTAINER
        )
        s3_key = self._storage.config.s3_key(
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            clip_id=clip_id,
            extension=extension,
        )
        thumb_key = (
            self._storage.config.thumbnail_key(
                tenant_id=req.tenant_id,
                session_id=req.session_id,
                clip_id=clip_id,
            )
            if (req.kind == "video" and self._thumbnail)
            else None
        )

        async with self._workdir() as workdir:
            clip_path = os.path.join(workdir, f"{clip_id}.{extension}")
            thumb_path = os.path.join(workdir, f"{clip_id}.jpg") if thumb_key else None

            await self._run_ffmpeg_cut(
                source_uri=source_uri,
                output_path=clip_path,
                kind=req.kind,
                start_ms=start_ms,
                duration_ms=end_ms - start_ms,
            )
            size_bytes = os.path.getsize(clip_path)
            if size_bytes > self._max_bytes:
                raise ClipExtractionFailed(
                    f"clip exceeds max_output_bytes "
                    f"({size_bytes} > {self._max_bytes})"
                )
            checksum = _sha256_file(clip_path)

            if thumb_path is not None:
                try:
                    await self._run_ffmpeg_thumbnail(
                        source_uri=source_uri,
                        output_path=thumb_path,
                        timestamp_ms=start_ms + (end_ms - start_ms) // 2,
                    )
                except ClipExtractionFailed as exc:
                    logger.warning(
                        "clip_service.thumbnail_failed: %s — continuing without it",
                        exc,
                    )
                    thumb_path = None
                    thumb_key = None

            content_type = (
                "video/mp4" if req.kind == "video" else "audio/mp4"
            )
            await self._storage.upload(
                local_path=clip_path,
                s3_key=s3_key,
                content_type=content_type,
            )
            if thumb_path is not None and thumb_key is not None:
                await self._storage.upload(
                    local_path=thumb_path,
                    s3_key=thumb_key,
                    content_type="image/jpeg",
                )

        duration_ms = end_ms - start_ms
        persisted = await self._persist_or_reuse_row(
            tenant_id=req.tenant_id,
            session_id=req.session_id,
            artifact_id=req.artifact_id,
            segment_id=req.segment_id,
            kind=req.kind,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=duration_ms,
            clip_id=clip_id,
            s3_key=s3_key,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=checksum,
            thumbnail_key=thumb_key,
        )

        signed = await self._storage.presigned_url(s3_key=persisted["s3_key"])
        thumb_signed = (
            await self._storage.presigned_url(s3_key=persisted["thumbnail_key"])
            if persisted["thumbnail_key"]
            else None
        )
        return Clip(
            clip_id=persisted["clip_id"],
            s3_bucket=persisted["s3_bucket"],
            s3_key=persisted["s3_key"],
            content_type=persisted["content_type"],
            duration_ms=persisted["duration_ms"],
            size_bytes=persisted["size_bytes"],
            thumbnail_key=persisted["thumbnail_key"],
            signed_url=signed,
            thumbnail_signed_url=thumb_signed,
            cached=persisted["clip_id"] != clip_id,
        )

    # ── Cache + persistence ────────────────────────────────────

    async def _lookup_cached(
        self,
        tenant_id: str,
        session_id: str,
        kind: str,
        start_ms: int,
        end_ms: int,
    ) -> Optional[dict]:
        async with self._db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    sa.select(media_clips).where(
                        media_clips.c.tenant_id == tenant_id,
                        media_clips.c.session_id == session_id,
                        media_clips.c.kind == kind,
                        media_clips.c.start_ms == start_ms,
                        media_clips.c.end_ms == end_ms,
                        media_clips.c.status == "ready",
                    )
                )
            ).mappings().first()
        return dict(row) if row else None

    async def _bump_hits(self, tenant_id: str, clip_id: str) -> None:
        async with self._db.tenant_session(tenant_id) as session:
            await session.execute(
                sa.update(media_clips)
                .where(
                    media_clips.c.tenant_id == tenant_id,
                    media_clips.c.clip_id == clip_id,
                )
                .values(
                    hits=media_clips.c.hits + 1,
                    last_served_at=datetime.now(timezone.utc),
                )
            )

    async def _persist_or_reuse_row(
        self, **kwargs: Any
    ) -> dict:
        tenant_id = kwargs["tenant_id"]
        now = datetime.now(timezone.utc)
        async with self._db.tenant_session(tenant_id) as session:
            stmt = pg_insert(media_clips).values(
                clip_id=kwargs["clip_id"],
                tenant_id=tenant_id,
                session_id=kwargs["session_id"],
                artifact_id=kwargs["artifact_id"],
                segment_id=kwargs["segment_id"],
                kind=kwargs["kind"],
                start_ms=kwargs["start_ms"],
                end_ms=kwargs["end_ms"],
                duration_ms=kwargs["duration_ms"],
                s3_bucket=self._storage.config.bucket,
                s3_key=kwargs["s3_key"],
                content_type=kwargs["content_type"],
                size_bytes=kwargs["size_bytes"],
                checksum_sha256=kwargs["checksum"],
                thumbnail_key=kwargs["thumbnail_key"],
                hits=1,
                status="ready",
                created_at=now,
                last_served_at=now,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_clip_window",
                set_={
                    "hits": media_clips.c.hits + 1,
                    "last_served_at": now,
                },
            ).returning(media_clips)
            row = (await session.execute(stmt)).mappings().first()
        return dict(row) if row else {}

    # ── ffmpeg subprocess management ───────────────────────────

    async def _run_ffmpeg_cut(
        self,
        *,
        source_uri: str,
        output_path: str,
        kind: str,
        start_ms: int,
        duration_ms: int,
    ) -> None:
        ss = _ms_to_ffmpeg_time(start_ms)
        t = _ms_to_ffmpeg_time(duration_ms)
        if kind == "video":
            args = [
                self._ffmpeg,
                "-y",
                "-nostdin",
                "-ss", ss,
                "-i", source_uri,
                "-t", t,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                output_path,
            ]
        else:  # audio
            args = [
                self._ffmpeg,
                "-y",
                "-nostdin",
                "-ss", ss,
                "-i", source_uri,
                "-t", t,
                "-vn",
                "-c:a", "aac",
                "-b:a", "128k",
                output_path,
            ]
        await self._run_subprocess(args, label="ffmpeg.cut")

    async def _run_ffmpeg_thumbnail(
        self,
        *,
        source_uri: str,
        output_path: str,
        timestamp_ms: int,
    ) -> None:
        args = [
            self._ffmpeg,
            "-y",
            "-nostdin",
            "-ss", _ms_to_ffmpeg_time(timestamp_ms),
            "-i", source_uri,
            "-frames:v", "1",
            "-q:v", "2",
            "-vf", "scale=320:-2",
            output_path,
        ]
        await self._run_subprocess(args, label="ffmpeg.thumbnail")

    async def _run_subprocess(
        self, args: list[str], *, label: str
    ) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ClipExtractionFailed(
                f"{label}: ffmpeg binary not found at {args[0]}"
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._max_wall
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            raise ClipExtractionFailed(
                f"{label}: timed out after {self._max_wall}s"
            )

        if proc.returncode != 0:
            tail = (stderr or b"")[-1024:].decode("utf-8", errors="replace")
            raise ClipExtractionFailed(
                f"{label}: exit={proc.returncode} stderr={tail}"
            )

    @asynccontextmanager
    async def _workdir(self) -> AsyncIterator[str]:
        path = tempfile.mkdtemp(prefix="nexus-clip-")
        try:
            yield path
        finally:
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:  # pragma: no cover — best-effort cleanup
                pass


# ── Helpers ─────────────────────────────────────────────────────


_TIME_PATTERN = re.compile(r"^\d{1,3}:[0-5]\d:[0-5]\d(\.\d+)?$|^\d+(\.\d+)?$")


def _ms_to_ffmpeg_time(ms: int) -> str:
    """Convert milliseconds to ffmpeg's HH:MM:SS.mmm format."""
    if ms < 0:
        ms = 0
    total_seconds = ms / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
