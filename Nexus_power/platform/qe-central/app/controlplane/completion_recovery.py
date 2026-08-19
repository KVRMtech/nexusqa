"""ORPHANED-COMPLETION RECOVERY — re-deliver a finished crawl whose callback was
lost (M1.7 / T-GW-02, qe-central side).

The explorer writes a durable ``completion.json`` into the crawl directory on the
shared volume BEFORE it attempts its callback, and writes a sibling
``completion.ack`` only once qe-central has accepted it.  A completion with no
ack is therefore a crawl that FINISHED and was never heard about — and, because
the volume is mounted on both sides, qe-central can see it.

This module is the delivery half of the reaper's reconciliation.

WHY IT RE-POSTS INSTEAD OF WRITING THE ROW ITSELF.  Completing a crawl does far
more than set a status: it maps the manifest to a bundle, writes the substrate,
promotes the artifact, imports the auth session, auto-generates test cases and
persists the discovered business rules.  A recovery path that shortcut all of
that would produce rows marked ``completed`` carrying none of the artefacts a
completion is supposed to produce — a NEW green-wash hole, opened by the code
closing the old one.  So the recovered body goes back through the ordinary
``POST /internal/crawls/{id}/complete`` seam, signed exactly as the explorer
would have signed it, and every downstream effect happens exactly once.

WHY A LOOPBACK HTTP CALL AND NOT AN IN-PROCESS FUNCTION CALL.  The signature
check, the crawl-to-tenant binding and the terminal-status idempotency guard all
live on that route.  Calling past them would mean a second, quieter ingest path
with a different set of safety properties — and the whole premise of this
milestone is that there is one way in and it is checked.  The cost is one
loopback request per recovered crawl, which happens only on a fleet that has
already lost a callback.

IDEMPOTENT AT BOTH ENDS.  The endpoint is a no-op on a crawl that already reached
a terminal state and returns 2xx for it, so a race between this and a genuinely
late callback ends with one completion and one no-op.  The ack is written on any
2xx, which takes the crawl off the orphan list either way.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from ..clients.config import phase1_settings

logger = logging.getLogger(__name__)

#: Written by the explorer when a crawl reaches a terminal state.
COMPLETION_FILENAME = "completion.json"
#: Written once qe-central has ACCEPTED the completion.  Its absence defines an
#: orphan, so it is never written optimistically.
ACK_FILENAME = "completion.ack"

#: Where qe-central reaches its OWN internal API.  Loopback by default: the
#: recovery call must not depend on the service being reachable from outside its
#: own container, and must not traverse the ingress that may be exactly what
#: dropped the original callback.
ENV_SELF_URL = "QEC_SELF_URL"
_DEFAULT_SELF_URL = "http://127.0.0.1:8093"

#: Bounded: the reaper ticks again, so a recovery that cannot land now will be
#: retried on the next sweep.  A long timeout here would stall the whole sweep
#: behind one sick crawl.
_TIMEOUT_S = 60.0


def self_base_url() -> str:
    return (os.environ.get(ENV_SELF_URL, "") or _DEFAULT_SELF_URL).rstrip("/")


def _crawl_dir(crawl_id: str) -> Path:
    """The per-crawl directory.

    ``crawl_id`` reaches here off an exploration row that qe-central itself
    minted and validated against ``CRAWL_ID_PATTERN`` at dispatch, so it holds no
    separators — but the join is still guarded below rather than trusted, because
    a path built from a stored value is exactly the kind of thing that stops
    being trustworthy when someone later adds a way to write that value.
    """
    return Path(phase1_settings.crawl_storage_root) / crawl_id


def _is_contained(crawl_id: str) -> bool:
    """Refuse any crawl id that would escape the storage root."""
    if not crawl_id or "/" in crawl_id or "\\" in crawl_id or crawl_id in (".", ".."):
        return False
    root = Path(phase1_settings.crawl_storage_root).resolve()
    try:
        return root in _crawl_dir(crawl_id).resolve().parents or \
            _crawl_dir(crawl_id).resolve().parent == root
    except OSError:                                          # pragma: no cover
        return False


def read_orphaned_completion(crawl_id: str) -> dict | None:
    """The durable completion body of a crawl that finished un-acknowledged.

    ``None`` when there is no record (the crawl genuinely did not finish — reap
    it), when it was already acknowledged, or when it cannot be parsed.  A
    corrupt record is treated as ABSENT rather than as partial truth: acting on
    half a completion is how a recovery path becomes a corruption path.
    """
    if not _is_contained(crawl_id):
        return None
    directory = _crawl_dir(crawl_id)
    completion = directory / COMPLETION_FILENAME
    if not completion.is_file() or (directory / ACK_FILENAME).is_file():
        return None
    try:
        loaded = json.loads(completion.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("qec.recovery.completion_unreadable",
                       extra={"crawl_id": crawl_id, "error": str(exc)[:200]})
        return None
    return loaded if isinstance(loaded, dict) else None


def mark_acknowledged(crawl_id: str, *, status: int = 200) -> None:
    """Write the ack so this crawl leaves the orphan list.

    Best-effort: a failed ack costs one redundant (and idempotent) re-delivery on
    the next sweep, which is strictly better than the alternative of writing the
    ack before knowing the delivery landed.
    """
    if not _is_contained(crawl_id):
        return
    try:
        (_crawl_dir(crawl_id) / ACK_FILENAME).write_text(
            json.dumps({"delivered": True, "status": int(status),
                        "by": "qe-central-reaper"}, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("qec.recovery.ack_failed",
                       extra={"crawl_id": crawl_id, "error": str(exc)[:200]})


async def redeliver_completion(crawl_id: str, body: dict) -> bool:
    """POST a recovered completion back through the ordinary ingest seam.

    Returns True when qe-central accepted it (and the ack has been written).

    SIGNED HERE, FRESHLY.  The v2 envelope carries a single-use nonce, so the
    explorer's original signature — if it even survived — could not be replayed.
    qe-central holds the same fleet secret it verifies with, so it can mint a
    valid envelope for a body it is re-delivering on the explorer's behalf.

    This is a privileged action and it is deliberately narrow: the body is one
    that an explorer already wrote to the shared volume, and it is delivered
    verbatim.  Nothing here composes, edits or invents a completion — if the file
    says the crawl failed, a failure is what gets delivered.
    """
    if not phase1_settings.explorer_token:
        logger.warning("qec.recovery.unsigned",
                       extra={"crawl_id": crawl_id,
                              "reason": "no fleet secret configured"})
        return False
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    url = "%s/internal/crawls/%s/complete" % (self_base_url(), crawl_id)
    try:
        signature = phase1_settings.sign_payload(payload, scope=f"complete:{crawl_id}")
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                url, content=payload,
                headers={"Content-Type": "application/json",
                         "X-QEC-Signature": signature,
                         "X-QEC-Token": phase1_settings.explorer_token},
            )
    except Exception as exc:
        logger.warning("qec.recovery.redeliver_failed",
                       extra={"crawl_id": crawl_id, "error": str(exc)[:300]})
        return False
    if 200 <= resp.status_code < 300:
        mark_acknowledged(crawl_id, status=resp.status_code)
        logger.warning(
            "qec.recovery.redelivered",
            extra={"crawl_id": crawl_id, "status": resp.status_code,
                   "note": "a completed crawl whose callback was lost has been "
                           "recovered from its durable completion manifest"},
        )
        return True
    # A 4xx will not resolve on retry (a body this service cannot route), but the
    # ack is deliberately NOT written: the crawl stays visible as an orphan so an
    # operator can see that something on the volume could not be ingested.
    logger.error("qec.recovery.redeliver_rejected",
                 extra={"crawl_id": crawl_id, "status": resp.status_code,
                        "detail": resp.text[:300]})
    return False


__all__ = [
    "COMPLETION_FILENAME", "ACK_FILENAME", "ENV_SELF_URL",
    "read_orphaned_completion", "redeliver_completion", "mark_acknowledged",
    "self_base_url",
]
