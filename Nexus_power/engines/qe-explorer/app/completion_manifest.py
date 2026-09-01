"""THE DURABLE COMPLETION MANIFEST — a finished crawl survives a dropped
callback (M1.7 / T-GW-02).

THE HOLE THIS CLOSES.  ``_fire_callback`` fired exactly once, and swallowed
every failure::

    except Exception as exc:
        logger.warning("qec.explorer.callback_failed ... (manifest at %s is
                       authoritative)", ...)

The parenthetical was aspirational.  The crawl manifest on the shared volume
really does hold the evidence, but NOTHING EVER READ IT unless a callback
arrived to point at it: qe-central learns a crawl finished only from the
callback, so a single lost POST — a rolling deploy, a 502 from the ingress, a
five-second network partition — orphaned a completed crawl permanently.  The
reaper then marked the row ``stalled``, which is honest but wrong: the crawl
did not stall, it finished, and its evidence was sitting on disk the whole time.

THE DESIGN.  Three files per crawl directory, written in a fixed order:

  ``completion.json``   the FULL callback body, fsynced, written BEFORE the
                        first delivery attempt.  This is the durable record that
                        the crawl reached a terminal state.  Its existence is
                        the fact; the POST is only a notification.
  ``completion.attempts`` an append-only delivery log (one JSON line per
                        attempt).  Makes recovery observable rather than
                        hidden — the milestone's non-functional requirement.
  ``completion.ack``    written ONLY after qe-central accepted the callback.
                        Its ABSENCE is what marks a crawl orphaned.

Recovery then has two independent legs, and neither depends on the other:

  * the explorer's own SWEEPER re-delivers un-acked manifests (this module's
    :func:`pending_completions`), so a crawl orphaned by a transient network
    failure recovers without qe-central noticing anything happened;
  * qe-central's REAPER reconciles the same files off the shared volume, so a
    crawl orphaned by the explorer PROCESS DYING mid-delivery still completes.

Both are idempotent: the receiving endpoint is already idempotent on terminal
status, and a duplicate delivery is a no-op that still writes the ack.

WRITES ARE ATOMIC.  ``completion.json`` is written to a temp file in the same
directory, fsynced, then ``os.replace``d into place — so a crash mid-write can
never leave a half-parsed completion record that a reader would treat as
authoritative.  The attempts log is append+fsync, matching :mod:`app.emit`.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

#: The durable completion record — the full callback body.
COMPLETION_FILENAME = "completion.json"
#: The delivery acknowledgement — written only when qe-central accepted.
ACK_FILENAME = "completion.ack"
#: Append-only delivery attempt log (JSONL).
ATTEMPTS_FILENAME = "completion.attempts"

#: A delivery is retried with exponential backoff.  Bounded: the sweeper picks up
#: anything these attempts do not land, so the in-line retry only has to survive
#: a blip, not a full outage.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_MAX_DELAY_S = 30.0


def crawl_dir(work_dir: str, crawl_id: str) -> Path:
    """The per-crawl directory — the SAME layout :mod:`app.emit` writes the crawl
    manifest into, so qe-central finds both under one path it already knows.

    Deliberately NOT ``emit.crawl_dir``: that one CREATES the directory as a side
    effect of being asked for it, which is right for a writer and wrong for every
    reader here.  :func:`pending_completions` walks a volume holding other crawls'
    directories, and a scan that mkdir'd its way through the filesystem would
    manufacture the very orphan directories it is supposed to be finding.
    """
    return Path(work_dir) / crawl_id


def completion_path(work_dir: str, crawl_id: str) -> Path:
    return crawl_dir(work_dir, crawl_id) / COMPLETION_FILENAME


def ack_path(work_dir: str, crawl_id: str) -> Path:
    return crawl_dir(work_dir, crawl_id) / ACK_FILENAME


def attempts_path(work_dir: str, crawl_id: str) -> Path:
    return crawl_dir(work_dir, crawl_id) / ATTEMPTS_FILENAME


def backoff_delay(attempt: int, *, base: float = DEFAULT_BASE_DELAY_S,
                  cap: float = DEFAULT_MAX_DELAY_S) -> float:
    """Exponential backoff for delivery ``attempt`` (1-based), capped.  PURE.

    Deliberately DETERMINISTIC — no jitter.  The explorer is single-flight, so
    there is no thundering herd of concurrent crawls to de-correlate, and a
    deterministic schedule is one a fault-injection test can assert exactly.
    """
    if attempt <= 1:
        return 0.0
    return min(float(cap), float(base) * (2 ** (attempt - 2)))


def _fsync_dir(path: Path) -> None:
    """fsync the DIRECTORY so a rename is itself durable.  Best-effort: not every
    filesystem (or Windows) permits opening a directory, and a crawl must not
    fail because its metadata flush was refused."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_completion(work_dir: str, crawl_id: str, body: dict) -> Path:
    """Durably record that this crawl reached a terminal state.  ATOMIC.

    Called BEFORE the first delivery attempt, always — including for a crawl that
    failed.  A failed crawl's completion is exactly as important to deliver as a
    successful one's: an orphaned failure is a row that spins in the UI forever.

    Raises ``OSError`` if the record cannot be made durable.  That propagates on
    purpose: if we cannot write the completion record we have no recovery path at
    all, and pretending otherwise is the green-wash this milestone removes.
    """
    directory = crawl_dir(work_dir, crawl_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(body, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), default=str)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(directory),
        prefix=".completion-", suffix=".tmp", delete=False,
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, str(completion_path(work_dir, crawl_id)))
    _fsync_dir(directory)
    return completion_path(work_dir, crawl_id)


def read_completion(work_dir: str, crawl_id: str) -> Optional[dict]:
    """The durable completion body, or ``None`` when there is none / it is
    unreadable.  A corrupt record is treated as ABSENT, never as partial truth."""
    path = completion_path(work_dir, crawl_id)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("qec.completion.unreadable crawl_id=%s error=%s",
                       crawl_id, str(exc)[:200])
        return None
    return loaded if isinstance(loaded, dict) else None


def record_attempt(work_dir: str, crawl_id: str, *, attempt: int, ok: bool,
                   status: int = 0, error: str = "") -> None:
    """Append one delivery attempt to the log.  Best-effort by design: losing an
    audit line must never prevent the delivery it was auditing."""
    entry = {"attempt": int(attempt), "ok": bool(ok),
             "status": int(status), "error": str(error)[:300]}
    try:
        path = attempts_path(work_dir, crawl_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        logger.warning("qec.completion.attempt_log_failed crawl_id=%s error=%s",
                       crawl_id, str(exc)[:200])


def read_attempts(work_dir: str, crawl_id: str) -> list[dict]:
    """Every recorded delivery attempt (a partial trailing line is discarded)."""
    path = attempts_path(work_dir, crawl_id)
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                break
    except OSError:
        return out
    return out


def mark_delivered(work_dir: str, crawl_id: str, *, status: int = 200) -> None:
    """Acknowledge that qe-central ACCEPTED this completion.

    Written only on a 2xx (or on the endpoint telling us it already holds a
    terminal state for this crawl — a duplicate is a successful delivery).  The
    ack's absence is the sole definition of an orphan, so writing it optimistically
    would re-open the exact hole this module closes.
    """
    directory = crawl_dir(work_dir, crawl_id)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with open(ack_path(work_dir, crawl_id), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"delivered": True, "status": int(status)},
                                    sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_dir(directory)
    except OSError as exc:
        logger.warning("qec.completion.ack_failed crawl_id=%s error=%s",
                       crawl_id, str(exc)[:200])


def is_delivered(work_dir: str, crawl_id: str) -> bool:
    return ack_path(work_dir, crawl_id).is_file()


def is_orphaned(work_dir: str, crawl_id: str) -> bool:
    """A crawl that reached a terminal state whose completion was never accepted."""
    return completion_path(work_dir, crawl_id).is_file() and not is_delivered(work_dir, crawl_id)


@dataclass(frozen=True)
class PendingCompletion:
    """An orphaned completion awaiting re-delivery."""

    crawl_id: str
    body: dict
    attempts: int = 0


def pending_completions(work_dir: str, *, limit: int = 200) -> list[PendingCompletion]:
    """Every durable completion on this volume that was never acknowledged.

    This is the explorer-side recovery scan, run at startup and on a slow timer.
    It is a pure directory walk — no DB, no crawl state — so it recovers crawls
    belonging to a PREVIOUS process instance, which is the whole point: the
    process that owned the delivery is exactly the one that died.

    Bounded by ``limit`` so a volume holding thousands of historical crawls
    cannot turn one sweep into an unbounded scan; the next sweep takes the rest.
    """
    root = Path(work_dir)
    if not root.is_dir():
        return []
    out: list[PendingCompletion] = []
    for entry in _iter_crawl_dirs(root):
        if len(out) >= limit:
            break
        crawl_id = entry.name
        if not is_orphaned(work_dir, crawl_id):
            continue
        body = read_completion(work_dir, crawl_id)
        if body is None:
            continue
        out.append(PendingCompletion(
            crawl_id=crawl_id, body=body,
            attempts=len(read_attempts(work_dir, crawl_id)),
        ))
    return out


def _iter_crawl_dirs(root: Path) -> Iterator[Path]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir():
                yield entry
        except OSError:
            continue


def completion_body_is_sane(body: Any) -> bool:
    """The minimum a re-delivery needs: which crawl, whose tenant, which row.

    A completion record missing any of these cannot be routed by qe-central, so
    the sweeper must not spend attempts on it — it is logged and left in place
    for an operator rather than retried into a permanent 404.
    """
    if not isinstance(body, dict):
        return False
    return bool(str(body.get("crawl_id") or "").strip()
                and str(body.get("tenant_id") or "").strip()
                and str(body.get("exploration_id") or "").strip())


__all__ = [
    "COMPLETION_FILENAME", "ACK_FILENAME", "ATTEMPTS_FILENAME",
    "DEFAULT_MAX_ATTEMPTS", "DEFAULT_BASE_DELAY_S", "DEFAULT_MAX_DELAY_S",
    "PendingCompletion", "backoff_delay", "crawl_dir", "completion_path",
    "ack_path", "attempts_path", "write_completion", "read_completion",
    "record_attempt", "read_attempts", "mark_delivered", "is_delivered",
    "is_orphaned", "pending_completions", "completion_body_is_sane",
]
