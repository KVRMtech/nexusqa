"""Client-readable projection of one persisted crawl coverage bundle.

The explorer's coverage account is deliberately detailed: it is evidence for a
catalogue fold, not a document a client should need to decode.  This module is
the narrow, value-free projection for the portal.  It never invents completion
or field coverage, and it deliberately never returns a field value: the report
may say *how* values were obtained, but not repeat what was entered into the
client's application.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in (value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ())
            if isinstance(row, Mapping)]


def _path(url: Any) -> str:
    """A readable page key without exposing a query string or fragment."""
    text = str(url or "").strip()
    if not text:
        return "(page not recorded)"
    try:
        parsed = urlsplit(text)
        return parsed.path or "/"
    except ValueError:
        return text.split("?", 1)[0].split("#", 1)[0]


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_near_misses(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Keep only labels and locations; a coverage report is never a data export."""
    allowed = ("field", "field_label", "label", "seed", "seed_label", "url")
    out: list[dict[str, str]] = []
    for row in rows:
        clean = {key: str(row.get(key) or "")[:300] for key in allowed
                 if str(row.get(key) or "").strip()}
        if clean:
            out.append(clean)
    return out


def build(coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the Team-E client coverage report from one live crawl bundle.

    The return shape is intentionally counts and labels only.  A missing section
    becomes an empty, explicit result rather than an exception or a made-up zero.
    """
    bundle = _mapping(coverage)
    summary = _mapping(bundle.get("flow_summary"))
    flows = _rows(bundle.get("flows"))
    page_rows: dict[str, dict[str, Any]] = {}

    for flow in flows:
        completed = bool(flow.get("journey_completed") or flow.get("completed"))
        flow_id = str(flow.get("flow_id") or "")[:120]
        for step in _rows(flow.get("steps")):
            url = str(step.get("url") or "")
            key = _path(url)
            page = page_rows.setdefault(key, {
                "page": key,
                "title": str(step.get("title") or "")[:300],
                "observed_steps": 0,
                "fields_filled": 0,
                "fields_unfilled": 0,
                "completed_journeys": set(),
            })
            page["observed_steps"] += 1
            page["fields_filled"] += _count(step.get("fields_filled"))
            page["fields_unfilled"] += _count(step.get("fields_unfilled"))
            if completed and flow_id:
                page["completed_journeys"].add(flow_id)

    pages = []
    for page in page_rows.values():
        pages.append({
            "page": page["page"], "title": page["title"],
            "observed_steps": page["observed_steps"],
            "fields_filled": page["fields_filled"],
            "fields_unfilled": page["fields_unfilled"],
            "completed_journeys": len(page["completed_journeys"]),
        })
    pages.sort(key=lambda row: row["page"])

    completed = _count(summary.get("journeys_completed"))
    found = _count(summary.get("flows_found"))
    account = {str(key): _count(value) for key, value in _mapping(bundle.get("data_account")).items()}
    near_misses = _safe_near_misses(_rows(bundle.get("seed_near_misses")))
    headline = (
        f"{completed} completed journey{'s' if completed != 1 else ''} from "
        f"{found} discovered flow{'s' if found != 1 else ''}."
    )
    return {
        "report_version": "team-e-client-coverage-v1",
        "headline": headline,
        "journeys": {
            "completed": completed,
            "found": found,
            "truncated": _count(summary.get("flows_truncated")),
            "branch_coverage": bool(summary.get("branch_coverage")),
            "branch_coverage_note": str(summary.get("branch_coverage_note") or "")[:500],
        },
        "pages": pages,
        "data_account": account,
        "seed_near_misses": near_misses,
        "notes": [
            "Counts come from the crawl bundle that produced this report.",
            "Entered values and seed values are intentionally excluded from this report.",
        ],
    }


__all__ = ["build"]
