"""Self-contained interactive HTML rendering of the Execution Evidence Report.

No external assets (works offline, inside an air-gapped on-prem review, and
inside the exported ZIP). No CDN, no fonts, no scripts fetched at view time —
a regulated reviewer opens one file.

Doctrine rendered, not just stored:
  * D4 — every rollup prints the FULL count triplet; there is no code path
    here that emits a single green badge;
  * D1 — evidence CLASS (PROVEN/INFERRED/UNVERIFIED), never a fabricated %;
  * D3 — machine-suggested fields are visibly marked "AI-suggested";
  * the Trust Block opens the document.
"""

from __future__ import annotations

import html
import json

from .evidence_report import (
    DEFECT_CASE_STATUSES, ST_BLOCKED, ST_CANCELLED, ST_COMPLETED_WITH_DEFECTS,
    ST_DEFECT, ST_DEFECT_HALTED, ST_EXEC_ERROR, ST_NEEDS_REVIEW,
    ST_NOT_EXECUTED, ST_PASSED, ST_SKIPPED, TRIPLET_KEYS,
)

_LABEL = {
    ST_PASSED: "Passed",
    ST_DEFECT: "Defect Found",
    ST_EXEC_ERROR: "Execution Error",
    ST_BLOCKED: "Blocked",
    ST_NEEDS_REVIEW: "Needs Review",
    ST_SKIPPED: "Skipped",
    ST_CANCELLED: "Cancelled",
    ST_COMPLETED_WITH_DEFECTS: "Completed with Defects",
    ST_DEFECT_HALTED: "Defect Found — Halted",
    ST_NOT_EXECUTED: "Not Executed",
}

# Semantic colour is independent of any brand accent: a defect (the product
# working correctly) must never read as an error (the product failing).
_CSS = """
:root{--bg:#0f1117;--panel:#161a23;--panel2:#1c2130;--tx:#e6e9ef;--dim:#98a2b3;
--line:#252b3a;--pass:#2ea36b;--defect:#c8871a;--err:#d1495b;--review:#6f7dd6;
--block:#7a6a4f;--skip:#5b6475;--accent:#4b7bec}
@media (prefers-color-scheme: light){:root{--bg:#f7f8fa;--panel:#fff;
--panel2:#f2f4f8;--tx:#182230;--dim:#5b6475;--line:#e2e6ee}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:17px;margin:30px 0 10px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:14px}
.trust{border-left:3px solid var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kv{background:var(--panel2);border-radius:8px;padding:9px 11px}
.kv .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.kv .v{font-size:15px;font-variant-numeric:tabular-nums;margin-top:2px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0}
.chip{border-radius:999px;padding:3px 10px;font-size:12px;font-weight:600;
font-variant-numeric:tabular-nums;border:1px solid transparent}
.c-passed{background:rgba(46,163,107,.14);color:var(--pass);border-color:rgba(46,163,107,.35)}
.c-defect_found{background:rgba(200,135,26,.14);color:var(--defect);border-color:rgba(200,135,26,.35)}
.c-execution_error{background:rgba(209,73,91,.14);color:var(--err);border-color:rgba(209,73,91,.35)}
.c-needs_review{background:rgba(111,125,214,.14);color:var(--review);border-color:rgba(111,125,214,.35)}
.c-blocked{background:rgba(122,106,79,.16);color:var(--block);border-color:rgba(122,106,79,.35)}
.c-skipped,.c-cancelled,.c-not_executed{background:rgba(91,100,117,.14);
color:var(--skip);border-color:rgba(91,100,117,.3)}
.c-zero{opacity:.42}
details{border:1px solid var(--line);border-radius:9px;margin-bottom:8px;
background:var(--panel);overflow:hidden}
details>summary{cursor:pointer;padding:11px 14px;list-style:none;display:flex;
gap:10px;align-items:center;flex-wrap:wrap}
details>summary::-webkit-details-marker{display:none}
details>summary:hover{background:var(--panel2)}
.name{font-weight:600}
.meta{color:var(--dim);font-size:12px}
.body{padding:4px 14px 14px;border-top:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11px;
text-transform:uppercase;letter-spacing:.05em;padding:7px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.scroll{overflow-x:auto}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.err{color:var(--err);white-space:pre-wrap;word-break:break-word;max-width:520px}
.tag{display:inline-block;background:var(--panel2);border:1px solid var(--line);
border-radius:5px;padding:1px 7px;font-size:11px;color:var(--dim);margin:0 4px 4px 0}
.ai{border:1px dashed var(--review);border-radius:7px;padding:9px 11px;margin-top:8px;
background:rgba(111,125,214,.06)}
.ai .hd{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--review);
font-weight:700;margin-bottom:4px}
.quote{border-left:2px solid var(--line);padding-left:9px;color:var(--dim);
white-space:pre-wrap;word-break:break-word}
.note{color:var(--dim);font-size:12px;margin-top:8px}
.ev{font-size:11px;font-weight:700;letter-spacing:.04em}
.ev-PROVEN{color:var(--pass)}.ev-INFERRED{color:var(--defect)}.ev-UNVERIFIED{color:var(--skip)}
"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


def _chips(counts: dict, keys=TRIPLET_KEYS) -> str:
    """D4 — ALWAYS the full breakdown. Zero-count buckets are dimmed, not
    hidden: 'we ran 47 and 0 were skipped' is a different claim from silence."""
    out = []
    for k in keys:
        n = int(counts.get(k, 0) or 0)
        z = " c-zero" if n == 0 else ""
        out.append(f'<span class="chip c-{_e(k)}{z}">{_LABEL.get(k, k)}: {n}</span>')
    ne = int(counts.get(ST_NOT_EXECUTED, 0) or 0)
    if ne:
        out.append(f'<span class="chip c-not_executed">Not Executed: {ne}</span>')
    return f'<div class="chips">{"".join(out)}</div>'


def _kv(k: str, v) -> str:
    return f'<div class="kv"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div></div>'


def _trust(t: dict) -> str:
    cert = t.get("certification_run") or {}
    rows = [
        _kv("Certified", "YES" if t.get("certified") else "NOT YET"),
        _kv("Suite size", t.get("suite_size")),
        _kv("Quarantined", t.get("quarantined_count")),
        _kv("Uncertified exploratory", t.get("uncertified_exploratory_count")),
    ]
    if cert:
        rows += [_kv("Cert steps", cert.get("total_steps")),
                 _kv("Cert passed", cert.get("passed_steps")),
                 _kv("Cert failed", cert.get("failed_steps")),
                 _kv("Cert skipped", cert.get("skipped_steps"))]
    body = [f'<div class="card trust"><h2 style="margin-top:0">Trust Block</h2>',
            f'<div class="note">{_e(t.get("statement"))}</div>',
            f'<div class="grid" style="margin-top:12px">{"".join(rows)}</div>']
    if cert.get("run_id"):
        body.append(f'<div class="note">Certification run '
                    f'<code>{_e(cert["run_id"])}</code> at {_e(cert.get("started_at"))}.</div>')
    q = t.get("quarantined") or []
    if q:
        items = "".join(f'<li><code>{_e(x.get("test_case_id"))}</code> — {_e(x.get("reason"))}</li>'
                        for x in q[:25])
        body.append(f'<div class="note"><b>Quarantined (excluded from client runs):</b>'
                    f'<ul>{items}</ul></div>')
    u = t.get("uncertified_exploratory") or []
    if u:
        items = "".join(f'<li><code>{_e(x.get("test_case_id"))}</code> — {_e(x.get("reason"))}</li>'
                        for x in u[:25])
        body.append(f'<div class="note"><b>Held by the exploratory gate:</b><ul>{items}</ul></div>')
    sc = t.get("oracle_scorecard") or {}
    if sc.get("grounding"):
        body.append('<div class="note"><b>Oracle grounding:</b> '
                    f'<code>{_e(json.dumps(sc.get("grounding")))}</code></div>')
    body.append("</div>")
    return "".join(body)


def _steps_table(steps: list) -> str:
    if not steps:
        return '<div class="note">No steps executed for this case in this run.</div>'
    head = ("<tr><th>#</th><th>Status</th><th>Action</th><th>Expected</th>"
            "<th>Actual / Error</th><th>Evidence</th><th>ms</th></tr>")
    body = []
    for s in steps:
        st = s.get("status", "")
        badge = f' <span class="meta">({_e(s.get("status_badge"))})</span>' if s.get("status_badge") else ""
        ev = s.get("evidence_class", "")
        an = s.get("analysis")
        ai = ""
        if an:
            quotes = "".join(f'<div class="quote">{_e(q)}</div>'
                             for q in (an.get("evidence_quoted") or [])[:3])
            ai = (f'<div class="ai"><div class="hd">AI-suggested analysis '
                  f'— pending human confirmation</div>'
                  f'<div><b>{_e(an.get("cause"))}</b>'
                  f'{" · " + _e(an.get("category")) if an.get("category") else ""}'
                  f'{" · " + _e(an.get("tier")) if an.get("tier") else ""}</div>'
                  f'<div class="note">{_e(an.get("detail"))}</div>{quotes}</div>')
        prov = s.get("oracle_provenance") or {}
        provbits = " ".join(
            f'<span class="tag">{_e(k)}:{_e(v)}</span>'
            for k, v in (("scene", prov.get("scene_id")), ("control", prov.get("control_id")),
                         ("edge", prov.get("edge_id")), ("prov", prov.get("recorded_provenance")))
            if v)
        shot = (s.get("evidence") or {}).get("screenshot_url") or ""
        shot_html = (f'<a href="{_e(shot)}">screenshot</a>' if shot else
                     '<span class="meta">—</span>')
        body.append(
            f'<tr><td class="mono">{_e(s.get("step_number"))}</td>'
            f'<td><span class="chip c-{_e(st)}">{_LABEL.get(st, st)}</span>{badge}</td>'
            f'<td>{_e(s.get("action"))}<div class="meta mono">{_e(s.get("target"))}</div></td>'
            f'<td>{_e(s.get("expected"))}<div class="ev ev-{_e(ev)}">{_e(ev)}</div>{provbits}</td>'
            f'<td><div class="err">{_e(s.get("actual"))}</div>{ai}</td>'
            f'<td>{shot_html}</td>'
            f'<td class="mono">{_e(s.get("duration_ms"))}</td></tr>')
    return f'<div class="scroll"><table>{head}{"".join(body)}</table></div>'


def _case(c: dict) -> str:
    st = c.get("status", "")
    repro = c.get("reproducibility") or {}
    rbits = "".join(f'<span class="tag">{_e(k)}: {_e(v)}</span>'
                    for k, v in repro.items() if v)
    tags = "".join(f'<span class="tag">{_e(t)}</span>' for t in (c.get("tags") or [])[:10])
    ne = (f'<div class="note"><b>Not executed:</b> {_e(c.get("not_executed_reason"))}</div>'
          if not c.get("executed") else "")
    return (
        f'<details><summary>'
        f'<span class="chip c-{_e(st)}">{_LABEL.get(st, st)}</span>'
        f'<span class="name">{_e(c.get("name"))}</span>'
        f'<span class="meta">{_e(c.get("test_type"))} · {_e(c.get("priority"))} · '
        f'{_e(c.get("steps_executed"))}/{_e(c.get("steps_declared"))} steps · '
        f'{_e(c.get("duration_ms"))} ms</span></summary>'
        f'<div class="body">{ne}'
        f'{_chips(c.get("counts") or {})}'
        f'<div class="note">{_e(c.get("description"))}</div>'
        f'<div class="note" style="margin-top:6px"><b>Reproducibility:</b> {rbits}'
        f'<span class="tag">case: {_e(c.get("test_case_id"))}</span></div>'
        f'<div style="margin:6px 0 10px">{tags}</div>'
        f'{_steps_table(c.get("steps") or [])}'
        f'</div></details>')


def _flow(f: dict) -> str:
    pp = f.get("pass_percentage")
    pp_s = f"{pp}%" if pp is not None else "—"
    cases = "".join(_case(c) for c in f.get("cases") or [])
    return (
        f'<details open><summary>'
        f'<span class="name">Flow: {_e(f.get("flow_label"))}</span>'
        f'<span class="meta">{_e(f.get("case_count"))} cases · pass {pp_s} · '
        f'defects {_e(f.get("defect_count"))} · {_e(f.get("duration_ms"))} ms</span>'
        f'</summary><div class="body">{_chips(f.get("counts") or {})}{cases}</div></details>')


def render_html(report: dict) -> str:
    """One self-contained HTML document for the whole report."""
    s = report.get("summary") or {}
    run = report.get("run") or {}
    cov = report.get("coverage") or {}
    head_kv = "".join([
        _kv("Artifact", s.get("artifact_id")),
        _kv("Run", run.get("run_id") or "—"),
        _kv("Environment", run.get("environment") or "—"),
        _kv("Run started", run.get("started_at") or "—"),
        _kv("Duration (ms)", run.get("duration_ms") or 0),
        _kv("Flows", s.get("total_flows")),
        _kv("Cases generated", s.get("total_cases_generated")),
        _kv("Cases executed", s.get("total_cases_executed")),
        _kv("Steps executed", s.get("total_steps_executed")),
    ])
    ne = cov.get("cases_not_executed") or []
    ne_html = ""
    if ne:
        items = "".join(f'<li>{_e(x.get("name"))} — <span class="meta">{_e(x.get("reason"))}</span></li>'
                        for x in ne[:60])
        ne_html = f'<ul>{items}</ul>'
    flows = "".join(_flow(f) for f in report.get("flows") or [])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Execution Evidence Report — {_e(s.get('artifact_id'))}</title>
<style>{_CSS}</style></head><body><div class="wrap">
<h1>Execution Evidence Report</h1>
<div class="sub">Certificate of Execution · report v{_e(report.get('report_version'))} ·
generated {_e(report.get('generated_at'))}</div>
{_trust(report.get('trust') or {})}
<h2>Execution Summary</h2>
<div class="card"><div class="grid">{head_kv}</div>
<div class="note" style="margin-top:12px"><b>Test cases</b></div>
{_chips(s.get('case_counts') or {})}
<div class="note" style="margin-top:10px"><b>Steps</b></div>
{_chips(s.get('step_counts') or {})}
<div class="note">Every bucket is shown, including zeros. A step that did not
execute is never counted as a pass.</div></div>
<h2>User Flows</h2>
{flows}
<h2>Coverage Honesty</h2>
<div class="card"><div class="note">{_e(cov.get('note'))}</div>
<div class="grid" style="margin-top:10px">
{_kv("Cases not executed", cov.get('cases_not_executed_count'))}
{_kv("Quarantined", cov.get('quarantined_count'))}
{_kv("Uncertified exploratory", cov.get('uncertified_exploratory_count'))}
</div>{ne_html}</div>
</div></body></html>"""


__all__ = ["render_html"]
