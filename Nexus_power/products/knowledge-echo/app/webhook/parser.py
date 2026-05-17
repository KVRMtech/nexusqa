"""Parse + validate inbound webhook bodies.

Contract (versioned via ``v``):
    {
      "v": 1,
      "tenant_id": "t1",                  // optional if the request
                                          //   is tenant-scoped via the URL
      "trigger": {
        "user_id": "ext-user-1",          // optional
        "channel_id": "ext-channel-1",    // optional
        "thread_id": "ext-thread-1"       // optional
      },
      "question": "How does X work?",     // required, 1..4000 chars
      "metadata": { ... }                 // optional, free-form
    }

We accept either ``application/json`` or already-decoded dicts. Any
extra top-level keys are rejected so payload schema drift is loud.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class WebhookInboundError(Exception):
    """The inbound body did not match the contract."""


class _Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: Optional[str] = Field(default=None, max_length=128)
    channel_id: Optional[str] = Field(default=None, max_length=128)
    thread_id: Optional[str] = Field(default=None, max_length=128)


class WebhookInboundPayload(BaseModel):
    """Validated inbound webhook payload."""

    model_config = ConfigDict(extra="forbid")

    v: int = Field(default=1, ge=1, le=1)
    tenant_id: Optional[str] = Field(default=None, max_length=64)
    trigger: _Trigger = Field(default_factory=_Trigger)
    question: str = Field(min_length=1, max_length=4000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("question must not be whitespace-only")
        return s


def parse_webhook_inbound(raw: bytes | str | dict[str, Any]) -> WebhookInboundPayload:
    """Return the validated payload or raise ``WebhookInboundError``."""
    if isinstance(raw, (bytes, bytearray)):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebhookInboundError(f"body is not UTF-8 JSON: {exc}") from exc
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise WebhookInboundError(f"body is not JSON: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise WebhookInboundError(
            f"unsupported body type: {type(raw).__name__}"
        )
    if not isinstance(data, dict):
        raise WebhookInboundError(
            f"body root must be an object, got {type(data).__name__}"
        )
    try:
        return WebhookInboundPayload.model_validate(data)
    except ValidationError as exc:
        raise WebhookInboundError(str(exc)) from exc
