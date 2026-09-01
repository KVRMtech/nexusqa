"""Ollama vision provider.

Wraps the local Ollama ``/api/chat`` endpoint with a multimodal payload.
Compatible models include ``llava:7b``, ``llama3.2-vision:11b``,
``moondream``, and any future Ollama-served multimodal model that accepts
the ``images: [base64]`` field on chat messages.

This is the Tier 3 default for on-prem / GPU-budget-constrained
deployments.  No network access required, no API key required, but
inference latency is bound to local hardware (~1-3 s per call on a
mid-range GPU; 30-180 s on CPU).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Optional

import httpx

from ..base import (
    VisionAnalysisRequest,
    VisionAnalysisResponse,
    VisionProvider,
    VisionProviderError,
    VisionTransitionRequest,
    VisionTransitionResponse,
    VisionUIElement,
)
from ..config import VisionTierSpec


_DEFAULT_BASE_URL = "http://localhost:11434"


class OllamaVisionProvider(VisionProvider):
    """Vision provider backed by a local Ollama installation."""

    def __init__(
        self,
        spec: VisionTierSpec,
        *,
        keep_alive: str = "10m",
        num_predict: int = 768,
        num_ctx: int = 4096,
    ):
        super().__init__(name=f"ollama:{spec.model}", model=spec.model, retries=spec.retries)
        self._spec = spec
        self._base_url = (spec.base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._timeout = spec.timeout_seconds
        self._keep_alive = keep_alive
        self._num_predict = num_predict
        self._num_ctx = num_ctx
        self._client: Optional[httpx.AsyncClient] = None

    async def initialize(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        # Probe the /api/tags endpoint so a misconfigured base_url surfaces
        # as a non-retriable error at startup instead of on every call.
        try:
            resp = await self._client.get("/api/tags", timeout=5.0)
            resp.raise_for_status()
        except Exception as exc:
            raise VisionProviderError(
                f"ollama tier{self._spec.tier} unreachable at {self._base_url}: {exc}",
                provider=self.name,
                retriable=False,
            ) from exc
        self._initialized = True

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._initialized = False

    async def health(self) -> dict[str, Any]:
        if self._client is None:
            return {"healthy": False, "provider": self.name, "reason": "not_initialized"}
        try:
            resp = await self._client.get("/api/tags", timeout=3.0)
            resp.raise_for_status()
        except Exception as exc:
            return {
                "healthy": False, "provider": self.name, "model": self.model,
                "reason": str(exc)[:200],
            }
        return {"healthy": True, "provider": self.name, "model": self.model}

    async def _do_analyze(self, request: VisionAnalysisRequest) -> VisionAnalysisResponse:
        if self._client is None:
            raise VisionProviderError(
                "ollama provider not initialized",
                provider=self.name,
                retriable=False,
            )

        prompt = _build_prompt(
            ocr_text=request.ocr_text,
            app_type=request.app_type,
            previous_description=request.previous_description,
        )

        try:
            image_b64 = _encode_image(request.frame_path)
        except FileNotFoundError as exc:
            raise VisionProviderError(
                f"frame not found: {request.frame_path}",
                provider=self.name,
                retriable=False,
            ) from exc
        except OSError as exc:
            raise VisionProviderError(
                f"failed to read frame {request.frame_path}: {exc}",
                provider=self.name,
                retriable=False,
            ) from exc

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": prompt, "images": [image_b64]},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": (
                    request.temperature
                    if request.temperature is not None
                    else 0.1
                ),
                "num_predict": (
                    request.max_tokens
                    if request.max_tokens is not None
                    else self._num_predict
                ),
                "num_ctx": self._num_ctx,
            },
        }

        try:
            resp = await self._client.post("/api/chat", json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"ollama timeout after {self._timeout}s",
                provider=self.name,
                retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"ollama HTTP error: {exc}",
                provider=self.name,
                retriable=True,
            ) from exc

        if resp.status_code >= 500:
            raise VisionProviderError(
                f"ollama returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"ollama returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name,
                retriable=False,
            )

        body = resp.json()
        content = body.get("message", {}).get("content", "")
        return _parse_provider_response(
            content_str=content,
            raw=body,
            model=self.model,
        )


    async def _do_analyze_transition(
        self, request: VisionTransitionRequest,
    ) -> VisionTransitionResponse:
        """Two-image transition analysis via Ollama multimodal chat.

        Ollama natively accepts multiple images on a single chat
        message via the `images: [base64, base64]` payload, so this
        is a straight single-request call (no chain-of-prompts).
        """
        if self._client is None:
            raise VisionProviderError(
                "ollama provider not initialized",
                provider=self.name,
                retriable=False,
            )

        try:
            b_before = _encode_image(request.before_frame_path)
            b_after = _encode_image(request.after_frame_path)
        except FileNotFoundError as exc:
            raise VisionProviderError(
                f"frame not found in transition request: {exc}",
                provider=self.name,
                retriable=False,
            ) from exc
        except OSError as exc:
            raise VisionProviderError(
                f"failed to read transition frames: {exc}",
                provider=self.name,
                retriable=False,
            ) from exc

        from ._transition_shared import (
            build_transition_prompt as _build_transition_prompt,
            parse_transition_response as _parse_transition_response_shared,
        )
        prompt = _build_transition_prompt(
            ocr_before=request.ocr_before,
            ocr_after=request.ocr_after,
            url_changed=request.url_changed,
        )

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": [b_before, b_after],
                },
            ],
            "stream": False,
            "format": "json",
            "keep_alive": self._keep_alive,
            "options": {
                "temperature": (
                    request.temperature
                    if request.temperature is not None
                    else 0.1
                ),
                "num_predict": (
                    request.max_tokens
                    if request.max_tokens is not None
                    else self._num_predict
                ),
                "num_ctx": self._num_ctx,
            },
        }

        try:
            resp = await self._client.post(
                "/api/chat", json=payload, timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise VisionProviderError(
                f"ollama transition timeout after {self._timeout}s",
                provider=self.name, retriable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionProviderError(
                f"ollama transition HTTP error: {exc}",
                provider=self.name, retriable=True,
            ) from exc

        if resp.status_code >= 500:
            raise VisionProviderError(
                f"ollama transition returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name, retriable=True,
            )
        if resp.status_code >= 400:
            raise VisionProviderError(
                f"ollama transition returned {resp.status_code}: {resp.text[:200]}",
                provider=self.name, retriable=False,
            )

        body = resp.json()
        content = body.get("message", {}).get("content", "")
        return _parse_transition_response_shared(
            content_str=content, raw=body, model=self.model,
        )


def _encode_image(frame_path: str) -> str:
    """Read an image file and base64-encode it for the Ollama API."""
    p = Path(frame_path)
    with p.open("rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _build_prompt(*, ocr_text: str, app_type: str, previous_description: str) -> str:
    """Build the JSON-output instruction for Ollama vision models.

    The prompt asks for the canonical four fields the rest of the pipeline
    expects.  ``previous_description`` enables delta prompting for
    per-frame analysis (Phase B.1) — when supplied the model is asked
    explicitly to describe what *changed* relative to the prior frame.
    """
    parts: list[str] = [
        "You are analyzing a screenshot from a software application for QA testing.",
    ]
    if app_type:
        parts.append(f"Application type: {app_type}.")
    if ocr_text:
        # Cap OCR injection at 1000 chars so the prompt stays cheap on Ollama
        # context.  Models do better with focused context anyway.
        parts.append(f"OCR text extracted from this frame: {ocr_text[:1000]}")
    if previous_description:
        parts.append(
            "PREVIOUS FRAME description for change-detection: "
            f"{previous_description[:600]}"
        )
        parts.append(
            "Focus on what CHANGED in this frame relative to the previous "
            "(typed values, selected options, button states, modal opens)."
        )
    parts.append(
        'Return ONLY a JSON object with keys: '
        '"ui_elements" (array of {element_type, text, confidence, properties}), '
        '"description" (string), '
        '"tables" (array; empty if none), '
        '"page_title" (string).'
    )
    parts.append(
        'element_type values: button, text_field, dropdown, label, table, '
        'menu, checkbox, radio, link, image, tab.'
    )
    return "\n".join(parts)


def _parse_provider_response(
    *, content_str: str, raw: dict[str, Any], model: str,
) -> VisionAnalysisResponse:
    """Parse a JSON-mode provider response into the canonical shape.

    Tolerates the common malformations cloud and local LLMs produce: bare
    arrays instead of objects, leading prose, fenced ``json`` code blocks.
    Falls back to an empty structure rather than raising — the caller can
    inspect ``raw_response`` and ``description`` to decide what to do.
    """
    parsed: Any
    try:
        parsed = json.loads(content_str)
    except json.JSONDecodeError:
        # Strip Markdown code fences and retry once.
        cleaned = content_str.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = {}

    if isinstance(parsed, list):
        parsed = {"ui_elements": parsed}
    elif not isinstance(parsed, dict):
        parsed = {}

    description = str(parsed.get("description", "") or "").strip()
    page_title = str(parsed.get("page_title", "") or "").strip()

    raw_elements = parsed.get("ui_elements") or []
    elements: list[VisionUIElement] = []
    if isinstance(raw_elements, list):
        for item in raw_elements:
            if not isinstance(item, dict):
                continue
            try:
                conf = float(item.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            props = item.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            elements.append(VisionUIElement(
                element_type=str(item.get("element_type", "") or "").strip(),
                text=str(item.get("text", "") or ""),
                confidence=max(0.0, min(1.0, conf)),
                properties=props,
            ))

    raw_tables = parsed.get("tables") or []
    tables: list[list[list[str]]] = []
    if isinstance(raw_tables, list):
        for table in raw_tables:
            if not isinstance(table, list):
                continue
            rows: list[list[str]] = []
            for row in table:
                if not isinstance(row, list):
                    continue
                rows.append([str(cell) for cell in row])
            if rows:
                tables.append(rows)

    return VisionAnalysisResponse(
        description=description,
        ui_elements=elements,
        tables=tables,
        page_title=page_title,
        model=model,
        raw_response=raw,
    )


# Transition prompt + parser live in ._transition_shared so every
# provider that implements _do_analyze_transition uses the identical
# wire shape. See that module for the canonical taxonomy.
