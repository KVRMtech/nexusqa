"""Evidence retention — bounded storage WITHOUT losing the audit trail.

Traces are megabytes each, so they cannot be kept forever; but deleting an
artifact outright would silently rewrite history — a report that once linked a
trace would simply show nothing, and no one could tell whether it never existed
or was quietly removed.

So retention here is **tombstoning, not deletion**:

  * past the hot window the BYTES are dropped, freeing the storage;
  * the row survives, carrying the artifact's SHA-256 digest, its original size
    and the date it was reclaimed;
  * anyone holding an exported copy can still prove it is the genuine artifact
    by hashing it against the retained digest.

Screenshots are kept far longer than traces (kilobytes vs megabytes) and the
tier-T3 diagnostics documents are small JSON, so each class gets its own window.
Nothing here ever touches the run/step rows themselves: the report's numbers,
statuses and attributions are permanent regardless of retention.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .run_screenshots import E2ERunScreenshotRow

logger = logging.getLogger(__name__)

#: Per-class hot windows in days. Traces dominate storage, so they reclaim
#: first; a screenshot is cheap and is what most reviewers actually open.
DEFAULT_WINDOWS = {
    "application/zip": 30,          # Playwright traces
    "application/json": 90,         # tier-T3 diagnostics documents
    "video/webm": 30,
    "video/mp4": 30,
    "image/png": 365,
    "image/jpeg": 365,
    "image/webp": 365,
}

#: A tombstoned blob is replaced by this marker, so the row is unambiguously a
#: reclaimed artifact rather than a corrupt or empty upload.
TOMBSTONE_PREFIX = b"NEXUS-EVIDENCE-RECLAIMED\n"


def window_days(content_type: str) -> int:
    """Hot window for a media type; env-overridable per class, e.g.
    ``NEXUS_RETENTION_APPLICATION_ZIP_DAYS=60``. 0 or negative = keep forever."""
    ct = (content_type or "").split(";")[0].strip().lower()
    env_key = "NEXUS_RETENTION_" + ct.replace("/", "_").replace("-", "_").replace(".", "_").upper() + "_DAYS"
    raw = (os.getenv(env_key, "") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("evidence_retention.bad_window env=%s value=%r", env_key, raw)
    return int(DEFAULT_WINDOWS.get(ct, 0))


def is_tombstone(blob: bytes | None) -> bool:
    return bool(blob) and bytes(blob).startswith(TOMBSTONE_PREFIX)


def _tombstone(digest: str, original_bytes: int, at: datetime) -> bytes:
    return (TOMBSTONE_PREFIX
            + f"sha256={digest}\n".encode()
            + f"original_bytes={original_bytes}\n".encode()
            + f"reclaimed_at={at.isoformat()}\n".encode()
            + b"note=The bytes were reclaimed under the retention policy. The\n"
              b"digest above still proves an exported copy is the genuine\n"
              b"artifact. The run, its steps and its verdicts are unaffected.\n")


async def apply_retention(
    session: AsyncSession, *, tenant_id: str, dry_run: bool = True,
    limit: int = 500, now: datetime | None = None,
    only_ids: list[str] | None = None,
) -> dict:
    """Reclaim evidence bytes past their window, leaving a verifiable tombstone.

    ``dry_run=True`` by default — a retention pass reports what it WOULD reclaim
    before anything is touched, because an irreversible sweep should never be
    the accidental result of calling a function.
    """
    now = now or datetime.now(timezone.utc)
    q = select(E2ERunScreenshotRow).where(E2ERunScreenshotRow.tenant_id == tenant_id)
    if only_ids:
        # Targeted reclaim: operate on exactly these artifacts and nothing else.
        # Useful for a scoped clean-up, and it is what lets the destructive path
        # be exercised end-to-end without putting real evidence at risk.
        q = q.where(E2ERunScreenshotRow.screenshot_id.in_(list(only_ids)[:1000]))
    rows = (await session.execute(
        q.order_by(E2ERunScreenshotRow.created_at.asc()).limit(limit)
    )).scalars().all()

    candidates: list[dict] = []
    reclaimed_bytes = 0
    for r in rows:
        ct = (r.content_type or "").split(";")[0].strip().lower()
        days = window_days(ct)
        if days <= 0:
            continue                      # keep-forever class
        created = r.created_at
        if created is None:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created > now - timedelta(days=days):
            continue                      # still inside the hot window
        blob = bytes(r.image or b"")
        if is_tombstone(blob):
            continue                      # already reclaimed
        candidates.append({
            "id": r.screenshot_id, "content_type": ct, "bytes": len(blob),
            "age_days": int((now - created).total_seconds() // 86400),
            "window_days": days,
        })
        reclaimed_bytes += len(blob)

    if not dry_run:
        for c in candidates:
            row = (await session.execute(
                select(E2ERunScreenshotRow).where(
                    E2ERunScreenshotRow.screenshot_id == c["id"],
                    E2ERunScreenshotRow.tenant_id == tenant_id,
                )
            )).scalar_one_or_none()
            if row is None or is_tombstone(bytes(row.image or b"")):
                continue
            digest = hashlib.sha256(bytes(row.image or b"")).hexdigest()
            stone = _tombstone(digest, len(bytes(row.image or b"")), now)
            await session.execute(
                update(E2ERunScreenshotRow)
                .where(E2ERunScreenshotRow.screenshot_id == c["id"],
                       E2ERunScreenshotRow.tenant_id == tenant_id)
                .values(image=stone, byte_size=len(stone),
                        content_type="application/vnd.nexus.tombstone")
            )
            c["sha256"] = digest

    by_class: dict[str, int] = {}
    for c in candidates:
        by_class[c["content_type"]] = by_class.get(c["content_type"], 0) + 1

    return {
        "dry_run": bool(dry_run),
        "scanned": len(rows),
        "candidates": len(candidates),
        "by_class": by_class,
        "reclaimable_bytes": reclaimed_bytes,
        "windows_days": {k: window_days(k) for k in sorted(DEFAULT_WINDOWS)},
        "scoped_to_ids": bool(only_ids),
        "items": candidates[:100],
        "note": ("Reclaimed evidence is TOMBSTONED, not deleted: the row survives "
                 "with the artifact's SHA-256, its original size and the date it "
                 "was reclaimed, so an exported copy can still be proven genuine. "
                 "Run/step rows, statuses and attributions are never touched — the "
                 "report's numbers do not change when storage is reclaimed."),
    }


__all__ = ["DEFAULT_WINDOWS", "TOMBSTONE_PREFIX", "window_days", "is_tombstone",
           "apply_retention"]
