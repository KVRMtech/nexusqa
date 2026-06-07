"""Script Factory (Stage 3) — deterministic Playwright generator.

Compiles the frozen, grounded ProductionTestCase (its OBSERVED per-step evidence:
verb, visible label, role/kind, value, spatial anchor, observed outcome,
confidence, provenance) into idiomatic, in-repo Playwright TypeScript that the
buyer OWNS.

Principles (enforced by construction):
* Deterministic, ZERO LLM — same case in, byte-identical code out.
* User-facing locators (getByRole/getByLabel/getByText), anchor-scoped; never
  absolute/index XPath.
* Real waits + semantic, step-anchored assertions from the observed outcome.
* Honest: un-observed (inferred/review) steps become flagged TODO(human) stubs,
  never invented. Fails toward red — no silent heal.

Additive: reads stored cases only; never modifies the frozen capture pipeline.
"""

from .compiler import compile_case, compile_project

__all__ = ["compile_case", "compile_project"]
