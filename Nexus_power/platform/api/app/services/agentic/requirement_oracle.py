"""Intent — the Requirement / Intent Oracle (P3 scaffold).

Generic + additive + never-green-wash. When a step is diagnosed a real regression (the
recorded outcome was contradicted), this checks the failure against the LINKED REQUIREMENT
(from the RTM / traceability) so a defect can cite *the violated requirement*, not just the
recorded outcome — the compliance evidence-chain a regulated buyer wants.

This is sequenced LAST (P3). It is implemented here as an inert, well-shaped scaffold:
gated OFF by default in the Governor, returns None unless a requirement is genuinely
linked, and never changes a verdict — it only ENRICHES a real-regression defect with a
grounded requirement citation. The RTM lookup is the wiring left for the P3 build.
"""
from __future__ import annotations

from . import governor

# Only a real-regression (a contradicted recorded outcome) gets a requirement citation;
# a script/data/env failure has no product requirement to violate.
_PRODUCT_CAUSES = frozenset({"REAL_REGRESSION"})


def applicable(diag: dict, *, prefs: dict | None = None) -> bool:
    """True iff the Intent oracle should run for this diagnosis: the agent is enabled AND
    the failure is a product regression (the only kind a requirement can be violated by)."""
    if not governor.agent_enabled("intent", prefs):
        return False
    cause = (diag or {}).get("cause", "").strip().upper()
    return cause in _PRODUCT_CAUSES


async def cite_requirement(diag: dict, *, requirement=None, prefs: dict | None = None) -> dict | None:
    """Enrich a real-regression diagnosis with the linked requirement citation.

    ``requirement`` is the already-resolved RTM record ({id, title, text, ...}) for this
    scenario/step — the P3 wiring resolves it from the traceability matrix. Returns a
    provenance-stamped citation dict to attach to the diagnosis, or None when inert (agent
    off, not a product regression, or no requirement linked). Never flips the verdict.
    """
    if not applicable(diag, prefs=prefs):
        return None
    req = requirement or {}
    rid = (req.get("id") or req.get("requirement_id") or "").strip()
    rtext = (req.get("text") or req.get("title") or "").strip()
    if not (rid or rtext):
        return None  # nothing linked -> inert (never fabricate a requirement)
    claim = (f"This contradicted outcome violates requirement {rid}: {rtext}".strip()
             if rid else f"This contradicted outcome violates the linked requirement: {rtext}")
    return {
        "requirement_id": rid,
        "requirement_text": rtext,
        "provenance": governor.stamp(
            claim, grounding=governor.G_LIVE_CONFIRMED,
            facts=[f"linked requirement {rid}".strip(), "recorded outcome was contradicted"],
        ),
    }
