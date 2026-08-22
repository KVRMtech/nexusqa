"""The provider half of A29, in its own process — and why it has to be.

``platform/api`` and ``engines/qe-explorer`` BOTH ship a top-level package
called ``app``. Importing the production LLM router therefore shadows the
Explorer's ``app.main``, and forcing them into one interpreter means editing
``sys.modules`` under two live packages — the exact "repo↔VM divergence was
sys.modules poisoning" failure this codebase has already paid for once.

A subprocess boundary costs one fork and removes the whole class of problem.
The router, the provider, the model call and the credentials all live on this
side; the parent sees only the model's reply text. Nothing about the call is
weakened by the split: this is the same ``build_router()`` and the same provider
code path ``platform-api`` runs.

Contract: argv[1] = a JSON request file
    {"system": str, "prompt": str, "image_path": str, "task": str,
     "model": str, "max_tokens": int}
stdout: one JSON object
    {"ok": bool, "text": str, "provider": str, "model": str,
     "finish_reason": str, "latency_s": float, "error": str}
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve()
NEXUS_ROOT = _HERE.parents[4]
sys.path.insert(0, str(NEXUS_ROOT / "platform" / "api"))


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print(json.dumps({"ok": False, "error": "OPENAI_API_KEY unset"}))
        return 2
    model = spec.get("model") or "gpt-4o"
    os.environ.update({
        "LLM_TIERS": "tier_vision",
        "LLM_DEFAULT_TIER": "tier_vision",
        "LLM_TIER_TIER_VISION_PROVIDER": "openai_compat",
        "LLM_TIER_TIER_VISION_BASE_URL": "https://api.openai.com/v1",
        "LLM_TIER_TIER_VISION_API_KEY": key,
        "LLM_TIER_TIER_VISION_MODEL": model,
        "LLM_TIER_TIER_VISION_TIMEOUT_S": "90",
    })
    import asyncio

    from app.services.llm import CompletionRequest
    from app.services.llm.router import build_router
    from app.services.llm.types import ImageContent

    router = build_router()
    image = Path(spec["image_path"]).read_bytes()
    req = CompletionRequest(
        system=spec.get("system", ""), prompt=spec.get("prompt", ""),
        max_tokens=int(spec.get("max_tokens") or 300), temperature=0.0,
        images=(ImageContent(data=image, media_type="image/png"),),
        metadata={"task": spec.get("task") or "vision_medic"},
    )

    async def run():
        started = time.monotonic()
        resp = await router.complete(task=spec.get("task") or "vision_medic",
                                     request=req)
        fr = getattr(resp, "finish_reason", "")
        return {
            "ok": True,
            "text": getattr(resp, "text", "") or "",
            "provider": getattr(resp, "provider", ""),
            "model": getattr(resp, "model", ""),
            "finish_reason": str(getattr(fr, "value", fr)),
            "error_detail": str(getattr(resp, "error_detail", "") or "")[:300],
            "latency_s": round(time.monotonic() - started, 2),
        }

    try:
        out = asyncio.run(run())
    except Exception as exc:                       # a provider failure is data
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
