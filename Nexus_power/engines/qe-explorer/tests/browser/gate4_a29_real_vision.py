"""A29 — ONE genuine multimodal prediction, executed by the Explorer, verified by R0.

WHAT WAS STUBBED, AND WHAT IS REAL NOW
======================================
Every vision proof before this one supplied the model's answer. The crawl was
handed a fixture prediction, or a coroutine returning fixed coordinates, and
what got exercised was the plumbing downstream of the model. That is worth
testing and it is not the claim A29 makes.

Here the coordinates are produced by a real multimodal model that has never seen
this page, from a screenshot taken seconds earlier, and nothing downstream knows
what they will be.

  REAL provider    ``platform/api/app/services/llm`` — the PRODUCTION router and
                   provider code, built by ``build_router()`` from the same
                   ``LLM_TIER_*`` environment contract platform-api reads.
  REAL prompt      ``platform/qe-central/app/services/vision_medic.SYSTEM``, the
                   authoritative prompt table entry for ``vision_medic``. Not a
                   prompt written for this test.
  REAL parser      the same ``parse_vision_proposal`` qe-central applies to the
                   model's reply, so a malformed answer fails here exactly as it
                   would in production.
  REAL redaction   ``pixel_redaction.redact_screenshot`` over regions located by
                   the production ``collect_pii_regions`` — the T-VIS-05 path.
                   If the mask cannot be proven, no image is sent.
  REAL browser     Playwright Chromium driving the production
                   ``PlaywrightBrowserPort``.
  REAL execution   ``port.click_at`` — including the page→viewport conversion
                   M3.1 had to fix.
  REAL verdict     R0 via ``verify_intent`` inside ``click_at``. Nothing here
                   decides whether the click worked; the port does.

WHAT IS NOT IN THE LOOP, STATED PLAINLY
=======================================
The two HTTP hops. In production the call travels
Explorer → qe-central ``/internal/vision-operate`` → platform-api
``/api/v1/llm/vision`` → provider. Here the Explorer-side caller and the
qe-central-side prompt/parse are exercised as CODE, and the provider call is
real, but the two services are not stood up as servers. Those hops are covered
separately: A28's tests pin the Explorer's request shape and signing, and
qe-central's own suite pins the endpoint. What A29 adds, and what nothing else
had, is that a real model's real answer moves a real browser and survives R0.

WHY FIXTURE 23
==============
It is a canvas application with NO readable interactive control — the DOM cannot
describe it, which is the only circumstance in which asking a model is
justified. Critically, its "Recalculate" control appends a real ``<button>`` to
the DOM when clicked, so the interactive signature CHANGES and R0 can verify on
the ordinary DOM rung. A correct prediction is therefore provable, and an
incorrect one (the inert decorative crest) is honestly unverifiable rather than
silently accepted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                            # pragma: no cover
    pass

_HERE = Path(__file__).resolve()
SERVICE_ROOT = _HERE.parents[2]
NEXUS_ROOT = SERVICE_ROOT.parents[1]
FIXTURES = SERVICE_ROOT / "tests" / "browser" / "fixtures"
# ONLY the explorer's own root. `platform/api` is deliberately NOT added:
# both ship a top-level `app` package, and importing the LLM router in this
# process would shadow `app.main`. The provider call runs in a child process
# (gate4_a29_predict.py) for exactly that reason.
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

FIXTURE = "23-canvas-app"
TARGET = "Recalculate"


def load_vision_medic():
    """qe-central's authoritative prompt + parser, loaded by path.

    Imported from the file rather than re-implemented so that if qe-central
    changes the prompt or the contract, this proof changes with it.
    """
    import importlib.util
    mod_path = (NEXUS_ROOT / "platform" / "qe-central" / "app" / "services"
                / "vision_medic.py")
    spec = importlib.util.spec_from_file_location("_vision_medic", mod_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_vision_medic"] = mod
    spec.loader.exec_module(mod)
    return mod


def call_real_provider(*, system: str, prompt: str, png: bytes,
                       task: str, model: str) -> dict:
    """Run the PRODUCTION router in a child process and return its reply."""
    import subprocess
    import tempfile
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("A29 needs a real provider: OPENAI_API_KEY is unset.")
    tmp = Path(tempfile.mkdtemp(prefix="a29-"))
    img = tmp / "shot.png"
    img.write_bytes(png)
    spec = tmp / "req.json"
    spec.write_text(json.dumps({
        "system": system, "prompt": prompt, "image_path": str(img),
        "task": task, "model": model, "max_tokens": 300}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-u", str(_HERE.parent / "gate4_a29_predict.py"),
         str(spec)],
        capture_output=True, text=True, timeout=180)
    line = ""
    for candidate in reversed((proc.stdout or "").strip().splitlines()):
        if candidate.strip().startswith("{"):
            line = candidate.strip()
            break
    if not line:
        return {"ok": False,
                "error": f"child produced no JSON: {(proc.stdout or '')[-300:]} "
                         f"{(proc.stderr or '')[-300:]}"}
    return json.loads(line)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--port", type=int, default=8721)
    args = ap.parse_args()

    import _harness as H                                    # noqa: E402
    from playwright.async_api import async_playwright

    from app.main import PlaywrightBrowserPort
    from app.pixel_redaction import redact_screenshot

    vm = load_vision_medic()

    findings: dict = {"milestone": "A29", "fixture": FIXTURE, "target": TARGET}
    server = H.FixtureServer(root=FIXTURES).start()
    try:
        url = server.url(FIXTURE)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1280,
                                                          "height": 900})
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(600)
            port = PlaywrightBrowserPort(page, context)

            # ── The DOM really cannot describe this page ────────────────────
            inventory = await port.collect_controls()
            findings["dom_controls_before"] = [
                {"name": c.get("name"), "kind": c.get("kind")}
                for c in (inventory or [])]

            # ── The image, through the production redaction path ────────────
            png = await port.screenshot_png()
            probe = await port.collect_pii_regions() or {}
            shot = redact_screenshot(png, list(probe.get("regions") or []),
                                     page_w=probe.get("page_w") or 0,
                                     page_h=probe.get("page_h") or 0,
                                     regions_ok=bool(probe.get("ok")))
            if shot is None:
                findings["verdict"] = ("INVALID - redaction could not be "
                                       "proven, so no image may be sent")
                return _emit(findings, args.out, 3)
            findings["image"] = {"sha256": shot.digest,
                                 "regions_masked": shot.regions_masked,
                                 "page_w": shot.page_w, "page_h": shot.page_h,
                                 "bytes": len(shot.png)}

            # The element bbox the medic reasons relative to: the canvas itself,
            # which is the whole opaque surface.
            box = await page.locator("canvas").first.bounding_box()
            scroll = await page.evaluate(
                "() => ({sx: window.scrollX||0, sy: window.scrollY||0})")
            page_box = {"x": box["x"] + scroll["sx"], "y": box["y"] + scroll["sy"],
                        "width": box["width"], "height": box["height"]}
            findings["bbox"] = page_box

            # ── THE REAL CALL ──────────────────────────────────────────────
            prompt = (
                f"The element is a {int(page_box['width'])}x"
                f"{int(page_box['height'])} canvas at page position "
                f"({int(page_box['x'])},{int(page_box['y'])}). The crawler needs "
                f"to operate the control labelled '{TARGET}'. Where should it "
                f"click?")
            reply = call_real_provider(
                system=vm.system_prompt_for(vm.TASK_VISION_MEDIC),
                prompt=prompt, png=shot.png, task=vm.TASK_VISION_MEDIC,
                model=os.environ.get("A29_MODEL", "gpt-4o"))
            raw_text = reply.get("text") or ""
            findings["model_call"] = {
                "provider": reply.get("provider", ""),
                "model": reply.get("model", ""),
                "finish_reason": reply.get("finish_reason", ""),
                "latency_s": reply.get("latency_s"),
                "raw_reply": raw_text[:600],
                "error": reply.get("error") or reply.get("error_detail") or "",
            }
            if not reply.get("ok") or not raw_text.strip():
                findings["verdict"] = ("FAIL - the provider returned no text; "
                                       "no prediction was produced")
                return _emit(findings, args.out, 1)

            # ── qe-central's own parser decides what the reply means ───────
            decision = vm.parse_vision_proposal(raw_text)
            findings["prediction"] = {
                "status": decision.status, "action": decision.action,
                "click_x": decision.click_x, "click_y": decision.click_y,
                "reason": (decision.reason or "")[:300],
            }
            if decision.status != vm.STATUS_PROPOSED:
                findings["verdict"] = (
                    f"NO PREDICTION - the model answered '{decision.status}'. "
                    f"That is an honest outcome the crawler handles, but it is "
                    f"not the prediction A29 requires.")
                return _emit(findings, args.out, 1)

            # ── Execute it. The coordinates are the model's, not the test's. ─
            px = int(round(page_box["x"] + decision.click_x))
            py = int(round(page_box["y"] + decision.click_y))
            findings["executed_page_point"] = {"x": px, "y": py}
            obs = await port.click_at(px, py)

            after = await port.collect_controls()
            findings["dom_controls_after"] = [
                {"name": c.get("name"), "kind": c.get("kind")}
                for c in (after or [])]
            findings["r0"] = {
                "intent_met": obs.intent_met,
                "dom_changed": obs.dom_changed,
                "url_before": obs.url_before, "url_after": obs.url_after,
                "error_detail": obs.error_detail,
            }
            verified = obs.intent_met is True
            findings["verdict"] = (
                f"PASS - {findings['model_call']['model']} predicted "
                f"({decision.click_x},{decision.click_y}) inside the canvas, the "
                f"Explorer clicked page ({px},{py}), and R0 VERIFIED the result "
                f"on the DOM rung." if verified else
                f"PREDICTION UNVERIFIED - the model proposed "
                f"({decision.click_x},{decision.click_y}); the click executed but "
                f"R0 reports intent_met={obs.intent_met}. This is the honest "
                f"outcome for a wrong coordinate, not a harness failure.")
            await browser.close()
            return _emit(findings, args.out, 0 if verified else 1)
    finally:
        server.stop()


def _emit(findings: dict, out: str, code: int) -> int:
    print("\n=== A29 - real multimodal prediction, executed and R0-verified ===")
    mc = findings.get("model_call", {})
    print(f"  provider={mc.get('provider')} model={mc.get('model')} "
          f"latency={mc.get('latency_s')}s")
    print(f"  DOM controls before the click: "
          f"{len(findings.get('dom_controls_before') or [])} "
          f"(a canvas app the DOM cannot describe)")
    print(f"  raw reply: {(mc.get('raw_reply') or '')[:200]}")
    p = findings.get("prediction")
    if p:
        print(f"  prediction: status={p['status']} "
              f"point=({p['click_x']},{p['click_y']}) reason={p['reason'][:120]}")
    if findings.get("executed_page_point"):
        print(f"  executed at page {findings['executed_page_point']}")
    r0 = findings.get("r0")
    if r0:
        print(f"  R0: intent_met={r0['intent_met']} dom_changed={r0['dom_changed']}")
        print(f"  DOM controls after: "
              f"{len(findings.get('dom_controls_after') or [])}")
    print(f"  VERDICT: {findings.get('verdict')}")
    if out:
        Path(out).write_text(json.dumps(findings, indent=2), encoding="utf-8")
        print(f"\nevidence -> {out}")
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
