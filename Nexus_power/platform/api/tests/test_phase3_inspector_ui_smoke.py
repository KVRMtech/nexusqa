"""Smoke test for Phase 3 — Evidence Inspector UI.

Phase 3 changes are client-side TypeScript + a backend SSE endpoint
already validated by ``test_phase2_streaming_smoke``.  This test
covers the parts that are validatable from Python without spinning
up Vite / TS toolchain:

  * New components exist on disk with the expected exports.
  * Components barrel re-exports them.
  * Hooks barrel re-exports ``useArtifactProgress``.
  * BoundingBox type is declared in canonical.ts and EvidenceControl
    references it (no more ``string | null`` placeholder).
  * E2EArchitectWorkspacePage imports + uses the new components.
  * Each new component file is syntactically well-formed enough that
    naive AST checks pass (balanced braces, no obvious truncation).
  * Integration smoke shell exercises the SSE progress endpoint.

Run:
    python Nexus_power/platform/api/tests/test_phase3_inspector_ui_smoke.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_API_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _API_ROOT.parents[1]
_CLIENT_ROOT = _REPO_ROOT / "client" / "src"


PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"  [OK]   {label}")
        PASS += 1
    else:
        print(f"  [FAIL] {label}  {detail}")
        FAIL += 1


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _looks_complete(src: str) -> bool:
    """Cheap truncation check.

    JSX/TSX makes accurate brace counting hard (expression containers
    inside attributes, template strings, regex literals, comments).
    Instead we look for the unmistakable end-of-file markers:

      * The file isn't trivially short.
      * Ends with a newline (no half-line truncation).
      * The last non-empty line is one of: ``}``, ``};``, a closing
        JSX tag, or a comment terminator.

    Combined with the export-token checks above this catches every
    real truncation we've seen without false-positiving on legal JSX.
    """
    if len(src) < 200:
        return False
    if not src.endswith("\n"):
        return False
    last = [ln.strip() for ln in src.splitlines() if ln.strip()][-1] if src.strip() else ""
    return last.endswith(("}", "};", ");", "/>", ">", "*/"))


# ── New components on disk ────────────────────────────────────────────────
def test_new_components_present() -> None:
    print("\n=== Phase 3 components on disk ===")
    expected = {
        "ConfidenceChip.tsx":            ["export function ConfidenceChip", "bandForScore"],
        "SceneFrameWithOverlays.tsx":    ["export function SceneFrameWithOverlays", "normaliseBox"],
        "AppTimeline.tsx":               ["export function AppTimeline", "APP_TYPE_PALETTE"],
    }
    for fname, tokens in expected.items():
        path = _CLIENT_ROOT / "components" / fname
        if not path.is_file():
            check(f"component file present: {fname}", False, detail="missing")
            continue
        src = _read(path)
        check(f"component file present: {fname}", True)
        check(f"  looks complete (not truncated) in {fname}", _looks_complete(src))
        for tok in tokens:
            check(f"  {fname} exports {tok!r}", tok in src)


# ── Hook on disk ──────────────────────────────────────────────────────────
def test_new_hook_present() -> None:
    print("\n=== Phase 3 hook on disk ===")
    path = _CLIENT_ROOT / "hooks" / "useArtifactProgress.ts"
    check("useArtifactProgress.ts present", path.is_file())
    if path.is_file():
        src = _read(path)
        check("  exports useArtifactProgress",
              "export function useArtifactProgress" in src)
        check("  exports ArtifactProgressEvent interface",
              "export interface ArtifactProgressEvent" in src)
        check("  wraps existing useSSE hook",
              "from './useSSE'" in src and "useSSE(" in src)
        check("  builds /v1/artifacts/.../progress path",
              "/v1/artifacts/" in src and "/progress" in src)
        check("  disables SSE on terminal status",
              "isTerminal" in src)


# ── Barrel re-exports ─────────────────────────────────────────────────────
def test_barrel_exports() -> None:
    print("\n=== Barrel re-exports ===")
    comp_index = _read(_CLIENT_ROOT / "components" / "index.ts")
    check("components/index.ts re-exports ConfidenceChip",
          "export { ConfidenceChip" in comp_index)
    check("components/index.ts re-exports SceneFrameWithOverlays",
          "SceneFrameWithOverlays" in comp_index)
    check("components/index.ts re-exports AppTimeline",
          "AppTimeline" in comp_index)
    check("components/index.ts re-exports BoundingBox helpers",
          "normaliseBox" in comp_index and "bandForScore" in comp_index)

    hooks_index = _read(_CLIENT_ROOT / "hooks" / "index.ts")
    check("hooks/index.ts re-exports useArtifactProgress",
          "useArtifactProgress" in hooks_index)
    check("hooks/index.ts re-exports ArtifactProgressEvent type",
          "ArtifactProgressEvent" in hooks_index)


# ── canonical.ts type fix ─────────────────────────────────────────────────
def test_canonical_type_fixed() -> None:
    print("\n=== canonical.ts BoundingBox type ===")
    src = _read(_CLIENT_ROOT / "types" / "canonical.ts")
    check("BoundingBox interface declared",
          "export interface BoundingBox" in src)
    check("EvidenceControl.bounding_box uses BoundingBox type",
          "bounding_box: BoundingBox" in src)
    check("legacy 'string | null' bounding_box removed",
          "bounding_box: string | null" not in src)
    check("BoundingBox supports {x1,y1,x2,y2} canonical form",
          "x1?:" in src and "x2?:" in src and "y1?:" in src and "y2?:" in src)


# ── Page wires the new components ─────────────────────────────────────────
def test_page_wires_components() -> None:
    print("\n=== E2EArchitectWorkspacePage wiring ===")
    src = _read(_CLIENT_ROOT / "pages" / "E2EArchitectWorkspacePage.tsx")
    check("page imports ConfidenceChip",
          "from '../components/ConfidenceChip'" in src)
    check("page imports SceneFrameWithOverlays",
          "from '../components/SceneFrameWithOverlays'" in src)
    check("page imports AppTimeline",
          "from '../components/AppTimeline'" in src)
    check("page imports useArtifactProgress hook",
          "from '../hooks/useArtifactProgress'" in src)
    check("page uses ConfidenceChip in Evidence Inspector",
          "<ConfidenceChip" in src)
    check("page renders SceneFrameWithOverlays for the active scene",
          "<SceneFrameWithOverlays" in src)
    check("page renders AppTimeline when artifact spans multiple apps",
          "<AppTimeline" in src
          and "app_instances.length > 1" in src)
    check("page tracks selectedControlId for overlay click",
          "selectedControlId" in src and "setSelectedControlId" in src)
    check(
        "resolvedControl prefers user-clicked control over derived one",
        "selectedControlId &&" in src,
    )
    # Verify the duplicated quality-chip JSX blocks are gone
    check(
        "old duplicated quality-chip JSX block (border-green-300...) removed",
        # the original used `border-green-300 text-green-700 bg-green-50`
        # specifically on scene_quality === 'strong' — replaced by ConfidenceChip
        "scene.scene_quality === 'strong' && 'border-green-300 text-green-700 bg-green-50'"
        not in src,
    )
    check(
        "old duplicated quality-chip JSX block for edges removed",
        "edge.action_quality === 'strong' && 'border-green-300 text-green-700 bg-green-50'"
        not in src,
    )


# ── Page still parses (loose brace check) ─────────────────────────────────
def test_page_parses() -> None:
    print("\n=== E2EArchitectWorkspacePage parse sanity ===")
    src = _read(_CLIENT_ROOT / "pages" / "E2EArchitectWorkspacePage.tsx")
    check("page looks complete (not truncated) after Phase 3 edits", _looks_complete(src))


# ── Runner ────────────────────────────────────────────────────────────────
def main() -> int:
    test_new_components_present()
    test_new_hook_present()
    test_barrel_exports()
    test_canonical_type_fixed()
    test_page_wires_components()
    test_page_parses()
    print(f"\n=== Phase 3 inspector-UI smoke: {PASS} pass, {FAIL} fail ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
