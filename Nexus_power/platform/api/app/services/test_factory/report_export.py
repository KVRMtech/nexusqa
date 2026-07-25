"""Export packaging + governance for the Execution Evidence Report (spec §2.16).

An export leaves the platform, so it is treated as an egress event, not a
download button:

  * **RBAC** — enforced by the route (viewers cannot export raw evidence).
  * **Redaction** — credential-shaped values are ALWAYS masked; masking every
    typed input is a per-export switch. Masked ≠ silently removed: the report
    says a value was redacted, so a reviewer is never misled into thinking a
    field was empty.
  * **Watermark** — who exported it, when, and from which artifact/run, stamped
    into the package and the HTML.
  * **Tamper-evidence** — a manifest of SHA-256 digests folded into a chain
    root, plus a dependency-free verifier script inside the package.
  * **Machine-readable verdict** — signed verdict JSON so CI gates and
    dashboards consume the SAME truth the humans read (JUnit XML cannot carry
    attribution classes or evidence provenance, so it is lossy by design).
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from .evidence_manifest import (
    MANIFEST_FILENAME, VERIFIER_FILENAME, VERIFIER_SCRIPT, build_manifest,
)

VERDICT_SCHEMA_VERSION = "nexus-verdict/v1"

#: Labels/actions whose values are ALWAYS masked, regardless of export options.
#: Deliberately generic (works on any app, any domain) — a word list here is a
#: safety net, never a product feature.
_CREDENTIAL_RX = re.compile(
    r"(pass(word|phrase)?|secret|token|api[_\- ]?key|auth|credential|otp|pin|"
    r"cvv|card[_\- ]?number|ssn|social[_\- ]?security|account[_\- ]?number)",
    re.IGNORECASE)

_MASK = "[REDACTED]"

# Value shapes that are sensitive wherever they appear.
_VALUE_RX = (
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"), "[REDACTED:email]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED:pan]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:ssn]"),
)


def _mask_value(text: str) -> str:
    out = str(text or "")
    for rx, repl in _VALUE_RX:
        out = rx.sub(repl, out)
    return out


def redact_report(report: dict, *, mask_all_inputs: bool = False) -> tuple[dict, dict]:
    """Return (redacted_report, stats).

    Credential-shaped fields are always masked. ``mask_all_inputs`` additionally
    masks every recorded action/target value — the stricter posture for an
    export that leaves the building.
    """
    import copy
    rep = copy.deepcopy(report)
    masked_fields = 0
    masked_values = 0

    def scrub_step(st: dict) -> None:
        nonlocal masked_fields, masked_values
        for key in ("action", "target", "expected", "actual", "resolved_selector"):
            val = st.get(key)
            if not isinstance(val, str) or not val:
                continue
            if _CREDENTIAL_RX.search(val):
                # Keep the ACTION verb legible, drop the payload after it.
                st[key] = _CREDENTIAL_RX.sub(lambda m: m.group(0), val)
                st[key] = re.sub(r"(['\"])(?:(?!\1).){1,200}\1", f"'{_MASK}'", st[key])
                masked_fields += 1
            new = _mask_value(st[key])
            if new != st[key]:
                masked_values += 1
                st[key] = new
            if mask_all_inputs and key in ("action", "target"):
                st[key] = re.sub(r"(['\"])(?:(?!\1).){1,200}\1", f"'{_MASK}'", st[key])
        an = st.get("analysis")
        if isinstance(an, dict) and an.get("evidence_quoted"):
            an["evidence_quoted"] = [_mask_value(q) for q in an["evidence_quoted"]]

    for flow in rep.get("flows") or []:
        for case in flow.get("cases") or []:
            for st in case.get("steps") or []:
                scrub_step(st)
    for d in ((rep.get("defects") or {}).get("defects") or []):
        d["fingerprint"] = _mask_value(d.get("fingerprint", ""))
        for occ in d.get("occurrences") or []:
            occ["error_excerpt"] = _mask_value(occ.get("error_excerpt", ""))

    stats = {"credential_fields_masked": masked_fields,
             "sensitive_values_masked": masked_values,
             "mask_all_inputs": bool(mask_all_inputs),
             "note": ("Redacted values are marked in place, never silently dropped — "
                      "a masked field must not read as an empty one.")}
    rep["redaction"] = stats
    return rep, stats


def build_verdict_json(report: dict) -> dict:
    """The machine-readable verdict: what a CI gate or dashboard should consume.

    Carries what JUnit XML structurally cannot — attribution class per failure,
    evidence class per assertion, defect signatures, and the certification
    (trust) state that says whether the suite had earned the right to judge.
    """
    summary = report.get("summary") or {}
    trust = report.get("trust") or {}
    run = report.get("run") or {}
    cases = []
    for flow in report.get("flows") or []:
        for c in flow.get("cases") or []:
            cases.append({
                "test_case_id": c.get("test_case_id"),
                "name": c.get("name"),
                "flow": flow.get("flow_key"),
                "type": c.get("test_type"),
                "status": c.get("status"),
                "executed": c.get("executed"),
                "counts": c.get("counts"),
                "duration_ms": c.get("duration_ms"),
                "not_executed_reason": c.get("not_executed_reason", ""),
                "failures": [
                    {"step_number": s.get("step_number"), "status": s.get("status"),
                     "badge": s.get("status_badge"),
                     "category": (s.get("analysis") or {}).get("category"),
                     "cause": (s.get("analysis") or {}).get("cause"),
                     "evidence_class": s.get("evidence_class"),
                     "suggested": (s.get("analysis") or {}).get("suggested", True)}
                    for s in (c.get("steps") or []) if s.get("status") != "passed"
                ],
            })
    return {
        "schema": VERDICT_SCHEMA_VERSION,
        "generated_at": report.get("generated_at"),
        "artifact_id": summary.get("artifact_id"),
        "run": {"run_id": run.get("run_id"), "environment": run.get("environment"),
                "started_at": run.get("started_at"),
                "ingested_totals": run.get("ingested_totals")},
        "trust": {"certified": trust.get("certified"),
                  "certification_run_id": (trust.get("certification_run") or {}).get("run_id"),
                  "quarantined_count": trust.get("quarantined_count"),
                  "uncertified_exploratory_count": trust.get("uncertified_exploratory_count")},
        "totals": {"case_counts": summary.get("case_counts"),
                   "step_counts": summary.get("step_counts"),
                   "cases_generated": summary.get("total_cases_generated"),
                   "cases_executed": summary.get("total_cases_executed")},
        "defects": [
            {"signature": d.get("signature"), "scenario_id": d.get("scenario_id"),
             "step_number": d.get("step_number"), "class": d.get("display_status"),
             "category": d.get("category"), "cause": d.get("cause"),
             "lifecycle": d.get("lifecycle"),
             "occurrence_count": d.get("occurrence_count"),
             "first_seen": d.get("first_seen"), "last_seen": d.get("last_seen")}
            for d in ((report.get("defects") or {}).get("defects") or [])
        ],
        "diff": report.get("diff"),
        "coverage": report.get("coverage"),
        "cases": cases,
        "doctrine": report.get("doctrine"),
        "gate_hint": {
            "note": ("Gate on execution_error/needs_review separately from "
                     "defect_found: an automation fault is OUR problem, an "
                     "application defect is a finding about the product under test."),
        },
    }


def watermark_html(html: str, *, exported_by: str, at: str, artifact_id: str,
                   run_id: str, chain_note: str) -> str:
    """Stamp provenance into the document itself, so a screenshot of the HTML
    still carries who exported it."""
    banner = (
        '<div style="position:sticky;top:0;z-index:9;background:#4b7bec;color:#fff;'
        'padding:7px 14px;font:12px/1.5 ui-sans-serif,system-ui,sans-serif">'
        f'Exported by <b>{exported_by}</b> at {at} · artifact {artifact_id} · '
        f'run {run_id or "—"} · {chain_note}</div>')
    idx = html.find("<body>")
    if idx < 0:
        return banner + html
    idx += len("<body>")
    return html[:idx] + banner + html[idx:]


def build_zip(*, report: dict, html: str, exported_by: str, artifact_id: str,
              run_id: str, mask_all_inputs: bool = False) -> tuple[bytes, dict]:
    """Assemble the complete evidence package.

    Contents: the human report (HTML), the full report JSON, the machine
    verdict JSON, a README, the manifest of digests, and the offline verifier.
    Returns (zip_bytes, manifest).
    """
    at = datetime.now(timezone.utc).isoformat()
    redacted, red_stats = redact_report(report, mask_all_inputs=mask_all_inputs)
    verdict = build_verdict_json(redacted)

    files: dict[str, bytes] = {}
    stamped = watermark_html(
        html, exported_by=exported_by, at=at, artifact_id=artifact_id,
        run_id=run_id, chain_note="integrity: see manifest.json + verify_evidence.py")
    files["report.html"] = stamped.encode("utf-8")
    files["report.json"] = json.dumps(redacted, indent=2, default=str).encode("utf-8")
    files["verdict.json"] = json.dumps(verdict, indent=2, default=str).encode("utf-8")
    files["README.txt"] = _readme(exported_by=exported_by, at=at,
                                  artifact_id=artifact_id, run_id=run_id,
                                  red_stats=red_stats).encode("utf-8")

    manifest = build_manifest(files, meta={
        "artifact_id": artifact_id, "run_id": run_id,
        "exported_by": exported_by, "exported_at": at,
        "report_version": report.get("report_version"),
        "verdict_schema": VERDICT_SCHEMA_VERSION,
        "redaction": red_stats,
    })

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, blob in files.items():
            z.writestr(path, blob)
        z.writestr(MANIFEST_FILENAME,
                   json.dumps(manifest, indent=2, default=str).encode("utf-8"))
        z.writestr(VERIFIER_FILENAME, VERIFIER_SCRIPT.encode("utf-8"))
    return buf.getvalue(), manifest


def _readme(*, exported_by: str, at: str, artifact_id: str, run_id: str,
            red_stats: dict) -> str:
    return f"""VKPower — Execution Evidence Export (Certificate of Execution)

Exported by : {exported_by}
Exported at : {at}
Artifact    : {artifact_id}
Run         : {run_id or "(latest)"}

CONTENTS
  report.html        the human-readable report (open in any browser, offline)
  report.json        the full structured report
  verdict.json       machine-readable verdict for CI gates and dashboards
  manifest.json      SHA-256 of every file above, folded into one chain root
  verify_evidence.py offline integrity verifier (Python standard library only)

VERIFY THIS PACKAGE
  python verify_evidence.py .

  Exits 0 only if every file matches its recorded digest and the chain root
  matches. Any modified byte in any file is reported by name.

WHAT THE STATUSES MEAN
  Passed                 the step ran and its recorded expectation held.
  Defect Found           the step ran and the APPLICATION UNDER TEST was found
                         wanting. This is a success of the testing product.
  Execution Error        the automation, environment or configuration failed.
                         This is OUR problem and is never reported as a defect
                         in your application.
  Needs Review           the step failed and no rung of the attribution ladder
                         could PROVE a cause. Routed to a human; never blamed
                         on the application by default.
  Blocked                the step did not run because an earlier step failed.
  Skipped / Cancelled    the step did not run. It is NEVER counted as a pass.

REDACTION
  Credential-shaped fields are always masked; mask_all_inputs={red_stats.get('mask_all_inputs')}.
  Fields masked: {red_stats.get('credential_fields_masked')} credential-shaped,
  {red_stats.get('sensitive_values_masked')} sensitive values.
  A masked value is marked in place — it never reads as an empty field.
"""


__all__ = ["VERDICT_SCHEMA_VERSION", "redact_report", "build_verdict_json",
           "watermark_html", "build_zip"]
