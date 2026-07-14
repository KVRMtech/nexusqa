"""Journey-graph builder (interface RESTORED after a repo divergence).

The original analytics module (multi-hop recovery, phantom-by-absence, deploy-impact
diffs) was built on a since-deleted environment and never committed to this repo, so
``test_factory.get_journey_graph`` imported a module that did not exist → 500. This
provides the two functions the router calls with a correct, minimal implementation:
pages become nodes and the recording's control-attributed transitions become edges,
so the endpoint returns a real (if un-analytic) graph instead of erroring.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def page_key(url: str) -> str:
    """Stable page key = the path (no scheme/host/query/fragment); '/' for the root."""
    s = (url or "").strip()
    if not s:
        return ""
    try:
        p = urlsplit(s)
        path = p.path if (p.scheme or p.netloc) else s.split("?")[0].split("#")[0]
        return (path or "/").rstrip("/") or "/"
    except Exception:
        return s


def build_journey_graph(pages: list, transitions: list) -> dict:
    """``{nodes, edges}`` — nodes from unique page keys (crawled pages + transition
    endpoints), edges from the control-attributed transitions."""
    order: list[str] = []
    seen: set[str] = set()

    def _add(k: str) -> None:
        if k and k not in seen:
            seen.add(k)
            order.append(k)

    for pg in (pages or []):
        _add(page_key(pg.get("url_path", "") if isinstance(pg, dict) else str(pg)))
    for t in (transitions or []):
        if isinstance(t, dict):
            _add(t.get("from_page") or "")
            _add(t.get("to_page") or "")

    return {
        "nodes": [{"id": k, "page": k} for k in order],
        "edges": [
            {
                "from": t.get("from_page", ""), "to": t.get("to_page", ""),
                "control_label": t.get("control_label", ""),
                "control_fp": t.get("control_fp", ""), "verb": t.get("verb", ""),
            }
            for t in (transitions or []) if isinstance(t, dict) and t.get("to_page")
        ],
        "node_count": len(order),
        "note": "minimal journey graph — analytic recovery/phantom features not on this build",
    }
