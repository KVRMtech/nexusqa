"""Environment governance (P4) — posture, production default-deny, behavior-class
scoping, and cardinality-driven repetition.

A RUN = Suite × Environment × Persona. P4 governs the ENVIRONMENT half and the
persona/journey fit:

  * POSTURE — every environment carries read_write | read_only | no_submit.
    A production environment is DEFAULT-DENY: a run is refused unless an explicit,
    revocable ``write_authorized`` is on file, and even then production floors the
    posture at no_submit-review — we never assume it is safe to mutate prod.
  * BEHAVIOR-CLASS SCOPING — the P3 structure diff proves that some cases belong
    to one behavior class (a 5-beneficiary journey a 2-kid persona never walks).
    Dispatching an out-of-class persona at such a case is REFUSED honestly (the
    test does not apply to this member), never run-and-failed as if the app broke.
  * CARDINALITY-DRIVEN REPETITION — a repeated block (beneficiary rows, dependents)
    runs as many times as THIS persona's data demands, from the proven count.

Pure functions, ZERO LLM, ZERO I/O. Every refusal carries an attribution that is
never the application — an out-of-scope case is ``test_scope``, a denied
production run is ``environment_policy``. Doctrine: never green-wash, never
false-red, never blame the app for a governance decision.
"""

from __future__ import annotations

POSTURE_READ_WRITE = "read_write"
POSTURE_READ_ONLY = "read_only"
POSTURE_NO_SUBMIT = "no_submit"
_POSTURES = (POSTURE_READ_WRITE, POSTURE_READ_ONLY, POSTURE_NO_SUBMIT)

# ordering from most permissive (0) to most restrictive (2); a floor takes max().
_RANK = {POSTURE_READ_WRITE: 0, POSTURE_NO_SUBMIT: 1, POSTURE_READ_ONLY: 2}

# attribution — a governance refusal is NEVER the application's fault.
ATTR_ENV_POLICY = "environment_policy"
ATTR_TEST_SCOPE = "test_scope"


def normalize_posture(posture: str | None) -> str:
    p = (posture or "").strip().lower()
    return p if p in _POSTURES else POSTURE_READ_WRITE


def effective_posture(env: dict | None) -> str:
    """The posture a run actually gets. A production environment floors the
    posture at no_submit even when configured read_write — writing to production
    requires the explicit gate in :func:`gate_dispatch`, never a bare posture."""
    env = env or {}
    declared = normalize_posture(env.get("posture"))
    if env.get("is_production"):
        # production can never be MORE permissive than no_submit via posture alone
        return declared if _RANK[declared] >= _RANK[POSTURE_NO_SUBMIT] else POSTURE_NO_SUBMIT
    return declared


def posture_forbids_submit(posture: str) -> bool:
    return normalize_posture(posture) in (POSTURE_READ_ONLY, POSTURE_NO_SUBMIT)


def gate_dispatch(env: dict | None, *, mutating_requested: bool) -> dict:
    """Decide whether a run may proceed against an environment, and under what
    posture. Production is default-deny.

    ``mutating_requested`` — does the caller intend the run to submit/mutate?
    (A read-only verification suite passes ``False`` and is allowed even on a
    locked environment.)

    Returns {allowed, posture, attribution, reasons[]}. When not allowed, the
    caller emits ZERO scripts and reports the attribution — never the app.
    """
    env = env or {}
    posture = effective_posture(env)
    reasons: list[str] = []
    is_prod = bool(env.get("is_production"))

    if is_prod:
        if not env.get("write_authorized") and mutating_requested:
            return {
                "allowed": False, "posture": posture, "attribution": ATTR_ENV_POLICY,
                "reasons": ["production is default-deny: no write authorization on "
                            "file for this environment, and the run intends to "
                            "mutate. This is an environment policy decision, not an "
                            "application failure."],
            }
        reasons.append("production environment — posture floored at "
                       f"'{posture}'; a mutating step is refused at run time.")

    if mutating_requested and posture_forbids_submit(posture):
        # allowed to RUN, but submit steps will be fenced; say so plainly.
        reasons.append(f"environment posture is '{posture}': submit/mutation steps "
                       "are fenced. Read-only verification proceeds; a mutation is "
                       "reported UNVERIFIED (fenced), never a false pass.")

    if env.get("health_status") in ("unreachable", "login_failed", "recipe_drift"):
        return {
            "allowed": False, "posture": posture, "attribution": ATTR_ENV_POLICY,
            "reasons": [f"environment last health check was "
                        f"'{env.get('health_status')}' ({env.get('health_detail') or 'no detail'}). "
                        "Refusing to dispatch against a known-bad environment — "
                        "this is an environment condition, not an app defect."],
        }

    return {"allowed": True, "posture": posture, "attribution": "",
            "reasons": reasons or [f"environment posture '{posture}'."]}


def scope_case(required_behavior_class: str, persona_behavior_class: str) -> dict:
    """Does a case (bound to a behavior class by a structure fork) apply to this
    persona? A case with no required class applies to everyone. A class-bound
    case applies only to an in-class persona; an out-of-class persona is REFUSED
    (test_scope), never run-and-failed.
    """
    req = (required_behavior_class or "").strip().lower()
    have = (persona_behavior_class or "").strip().lower()
    if not req:
        return {"in_scope": True, "attribution": "", "reason": "case is not class-bound."}
    if req == have:
        return {"in_scope": True, "attribution": "",
                "reason": f"persona is in the '{req}' behavior class this case demonstrates."}
    return {
        "in_scope": False, "attribution": ATTR_TEST_SCOPE,
        "reason": (f"this case belongs to the '{req}' behavior class; the persona is "
                   f"'{have or 'unclassified'}'. The journey does not apply to this "
                   "member — skipped as out-of-scope, NOT failed. Nothing about the "
                   "application is asserted."),
    }


def partition_suite(cases_required_class: dict[str, str],
                    persona_behavior_class: str) -> dict:
    """Split a suite's scenarios into in-scope vs out-of-scope for a persona.

    ``cases_required_class`` maps scenario_id → required behavior class ('' = any).
    Returns {in_scope[], out_of_scope[{scenario_id, reason}]}.
    """
    in_scope: list[str] = []
    out_of_scope: list[dict] = []
    for sid, req in (cases_required_class or {}).items():
        d = scope_case(req, persona_behavior_class)
        if d["in_scope"]:
            in_scope.append(sid)
        else:
            out_of_scope.append({"scenario_id": sid, "attribution": d["attribution"],
                                 "reason": d["reason"]})
    return {"in_scope": in_scope, "out_of_scope": out_of_scope,
            "in_scope_count": len(in_scope), "out_of_scope_count": len(out_of_scope)}


def repetition_count(proven_counts: dict[str, int], block: str,
                     persona_id: str, default: int = 1) -> int:
    """How many times a cardinality-driven block repeats for a persona. Uses the
    PROVEN per-persona count (e.g. beneficiaries) when known, else the default.
    Never invents a count — an unknown block repeats once (the recorded shape)."""
    counts = proven_counts or {}
    key = f"{persona_id}::{block}"
    for k in (key, block):
        v = counts.get(k)
        if isinstance(v, int) and v > 0:
            return v
    return max(1, int(default or 1))


def repetition_plan(cardinality_differences: list[dict],
                    proven_counts: dict[str, int], persona_id: str) -> list[dict]:
    """For each cardinality-varying block (from the P3 structure diff), the count
    THIS persona should run. One suite, any count — never a hardcoded 5."""
    plan: list[dict] = []
    for cd in (cardinality_differences or []):
        block = str(cd.get("block") or "")
        if not block:
            continue
        n = repetition_count(proven_counts, block, persona_id,
                             default=int(cd.get("count_b") or cd.get("count_a") or 1))
        plan.append({"block": block, "repeat": n})
    return plan


__all__ = [
    "POSTURE_READ_WRITE", "POSTURE_READ_ONLY", "POSTURE_NO_SUBMIT",
    "ATTR_ENV_POLICY", "ATTR_TEST_SCOPE",
    "normalize_posture", "effective_posture", "posture_forbids_submit",
    "gate_dispatch", "scope_case", "partition_suite",
    "repetition_count", "repetition_plan",
]
