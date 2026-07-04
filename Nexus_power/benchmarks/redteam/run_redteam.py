#!/usr/bin/env python3
"""RED-TEAM AGENT — adversarial self-test of the verification gate.

Seeds KNOWN-BAD specs (the exact defect classes the gate exists to catch) plus
one known-good control, runs the same deterministic rubric + lint the delivery
gate uses, and FAILS (exit 1) if any bad asset would certify or the good
control would be blocked. Run in CI / nightly:

    docker exec -w /app/service nexus-platform-api \\
        python /app/service/../benchmarks/redteam/run_redteam.py   # or copy in

Never green-wash applied to ourselves: the verifier is only trustworthy if it
provably cannot be evaded by the failure modes it claims to catch."""
import sys

sys.path.insert(0, "/app/service")
from app.services.test_factory import playwright_auditor as P  # noqa: E402


def step(n, action, prov="demonstrated", verb="click", url="", value="", label="", next_url=""):
    return {"step_number": n, "action": action, "provenance": prov,
            "expected": action,
            "observed": {"verb": verb, "url": url, "value": value,
                          "label": label, "next_url": next_url}}


class Act:
    def __init__(self, label, value, verb="type"):
        self.target_label, self.value, self.verb = label, value, verb


CASES = [
    {
        "name": "impossible-navigation (click asserts URL the recording never showed)",
        "spec": "await page.goto('/a');\nawait expect(page).toHaveURL(/checkout/);",
        "steps": [step(1, "Open /a", verb="navigate", url="/a"),
                  step(2, "Click 'Buy'", verb="click", next_url="/checkout", prov="")],
        "evidence": None,
        "expect_certified": False,
    },
    {
        "name": "dropped recorded fill (user typed a value the spec never replays)",
        "spec": "await page.goto('/form');",
        "steps": [step(1, "Open /form", verb="navigate", url="/form")],
        "evidence": [Act("Coupon code", "SAVE20")],
        "expect_certified": False,
    },
    {
        "name": "forbidden flaky APIs (sleep + page.click + ElementHandle)",
        "spec": ("await page.goto('/x');\nawait page.waitForTimeout(3000);\n"
                 "await page.click('#btn');\nconst h: ElementHandle = await page.$('#y');"),
        "steps": [step(1, "Open /x", verb="navigate", url="/x")],
        "evidence": None,
        "expect_certified": None,  # judged on lint errors instead
        "expect_lint_errors_min": 3,
    },
    {
        "name": "same-page green-wash (toHaveURL asserts the page it is already on)",
        "spec": "await expect(page).toHaveURL(/inventory/);",
        "steps": [step(1, "Verify inventory", verb="navigate",
                       url="/inventory", next_url="/inventory", prov="")],
        "evidence": None,
        "expect_certified": False,
        "expect_max_overall": 8,
    },
    {
        "name": "GOOD CONTROL (grounded fill, no violations) must NOT be blocked",
        "spec": ("import { test, expect } from '@playwright/test';\n"
                 "await page.goto('/login');\n"
                 "const f = page.getByLabel('Email');\nawait f.fill('user@example.com');\n"
                 "await expect(f).toHaveValue(/user/i);"),
        "steps": [step(1, "Open /login", verb="navigate", url="/login"),
                  step(2, "Enter 'user@example.com' in the 'Email' field",
                       verb="fill", value="user@example.com", label="Email")],
        "evidence": [Act("Email", "user@example.com")],
        "expect_certified": True,
    },
]


def main() -> int:
    failures = []
    for c in CASES:
        det = P.score_spec(c["spec"], c["steps"], evidence=c["evidence"])
        try:
            lint = P.lint_spec(c["spec"])
        except Exception:
            lint = []
        lint_err = sum(1 for l in lint if l.get("severity") == "error")
        certified = det.get("decision") == "certified" and int(det.get("overall_score") or 0) >= 9
        ok = True
        why = []
        if c.get("expect_certified") is True and not certified:
            ok = False
            why.append(f"good control blocked: decision={det.get('decision')} overall={det.get('overall_score')}")
        if c.get("expect_certified") is False and certified:
            ok = False
            why.append("BAD ASSET WOULD CERTIFY — gate evaded")
        if "expect_lint_errors_min" in c and lint_err < c["expect_lint_errors_min"]:
            ok = False
            why.append(f"lint caught {lint_err} < {c['expect_lint_errors_min']} expected")
        if "expect_max_overall" in c and int(det.get("overall_score") or 0) > c["expect_max_overall"]:
            ok = False
            why.append(f"overall {det.get('overall_score')} > allowed {c['expect_max_overall']}")
        status = "CAUGHT" if (ok and c.get("expect_certified") is not True) else ("PASS" if ok else "EVADED")
        print(f"[{'OK ' if ok else 'FAIL'}] {status:<7} {c['name']}"
              f"  (overall={det.get('overall_score')}, decision={det.get('decision')}, lint_err={lint_err})")
        if not ok:
            failures.append((c["name"], why))
    if failures:
        print("\nRED-TEAM FAILURES:")
        for name, why in failures:
            print(" -", name, "->", "; ".join(why))
        return 1
    print(f"\nRED-TEAM PASS: {len(CASES)-1} attack classes caught, good control certified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
