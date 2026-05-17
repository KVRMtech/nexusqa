"""Webhook composer — minimal JSON envelope.

The webhook surface returns a generic structured payload that the
receiving system can render however it likes. We include the top
candidate's text + audit metadata so any consumer can build a
useful UX without re-querying the platform.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..matcher import MatchResult
from ..surfaces import ComposedPayload, SurfaceComposer


class WebhookComposer:
    """Builds the JSON envelope sent to the tenant's webhook URL."""

    def __init__(self, *, schema_version: int = 1):
        if schema_version < 1:
            raise ValueError("schema_version must be >= 1")
        self._schema = schema_version

    def compose(
        self,
        *,
        dispatch_id: str,
        question_text: str,
        match: MatchResult,
    ) -> Optional[ComposedPayload]:
        if match.is_empty:
            return None
        top = match.candidates[0]
        similarity_pct = int(
            round(max(0.0, min(1.0, top.similarity)) * 100)
        )
        text = (
            f"Knowledge Echo {similarity_pct}% — "
            f"\"{_truncate(top.text, 200)}\""
        )
        payload: dict[str, Any] = {
            "v": self._schema,
            "type": "knowledge_echo",
            "dispatch_id": dispatch_id,
            "question": question_text,
            "match": {
                "similarity": top.similarity,
                "similarity_pct": similarity_pct,
                "confidence_band": match.confidence_band,
                "primary": {
                    "node_id": top.node_id,
                    "node_type": top.node_type,
                    "text": top.text,
                    "speaker_id": top.speaker_id,
                    "speaker_role": top.speaker_role,
                    "session_id": top.session_id,
                    "artifact_id": top.artifact_id,
                    "start_ms": top.start_ms,
                    "end_ms": top.end_ms,
                    "product_ids": list(top.product_ids),
                },
                "candidates": [c.to_audit_dict() for c in match.candidates],
            },
        }
        return ComposedPayload(
            surface="webhook",
            text=text,
            payload=payload,
            payload_hash=_hash_payload(payload),
            similarity_pct=similarity_pct,
            primary_candidate=top.to_audit_dict(),
        )


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


def _hash_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
