"""Canonical Trust Baseline — FIRST MEASURED RUN on real production data (M1A).

Loads the REAL extraction (page_visits + page_actions) and the REAL generated test
case for two recordings whose ground truth we know — saucedemo (9cef3242) and the
Aegis insurance flow (7a0b36a6) — builds hand-verified ground-truth labels, and scores
each with the harness. This converts "~4/10 opinion" into MEASURED numbers.

Run:  cd platform/api && python tests/accuracy/baseline.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from harness import Action, CanonicalDoc, Edge, PageNode, aggregate, score  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")
SAUCE = "9cef3242-8f5e-4537-bef6-562c9bcd679e"
AEGIS = "7a0b36a6-241b-410a-bcc5-a634b70f2cab"


def _read(aid: str, kind: str):
    with open(os.path.join(FX, f"{aid}_{kind}.json"), "r", encoding="utf-8") as fh:
        txt = fh.read().strip()
    return json.loads(txt) if txt and txt != "" else []


def _page_key(v: dict) -> str:
    return (v.get("url_path") or v.get("location") or "").strip()


# ── Adapter: real extraction (visits + actions) -> CanonicalDoc ────────────────

def extraction_doc(aid: str) -> CanonicalDoc:
    visits = _read(aid, "visits")
    actions = _read(aid, "actions")
    seq_key = {v["seq"]: _page_key(v) for v in visits}
    nodes = [PageNode(page_key=_page_key(v), location=v.get("location", ""),
                      confidence=float(v.get("conf") or 0), source=v.get("source", ""))
             for v in visits]
    # Keep SEMANTIC actions only: drop scroll/none (not test steps) and a focus-click
    # that immediately precedes a type/select on the SAME label (clicking to focus a
    # field, then typing — one intent, not two). This measures the semantic extraction.
    acts = []
    for i, a in enumerate(actions):
        verb = a.get("verb", "")
        if verb in ("scroll", "none", "hover"):
            continue
        if verb == "click" and i + 1 < len(actions):
            nx = actions[i + 1]
            if nx.get("verb") in ("type", "select", "fill") and nx.get("label") == a.get("label"):
                continue
        acts.append(Action(
            verb=verb, target_label=a.get("label", "") or "",
            value=(a.get("value") or ""), page_key=seq_key.get(a.get("seq"), ""),
            confidence=float(a.get("conf") or 0), automation_ready=bool(a.get("ar")),
            source="extraction"))
    # edges = consecutive DISTINCT page_keys in the visit sequence
    edges, prev = [], None
    for v in sorted(visits, key=lambda x: x["seq"]):
        k = _page_key(v)
        if prev is not None and k and prev != k:
            edges.append(Edge(prev, k))
        prev = k
    return CanonicalDoc(page_nodes=nodes, actions=acts, edges=edges)


# ── Adapter: real generated test case -> CanonicalDoc ─────────────────────────

def testcase_doc(aid: str) -> CanonicalDoc:
    raw = _read(aid, "testcase")
    tc = raw if isinstance(raw, dict) else {}
    steps = tc.get("steps") or []
    acts, nodes_seen, edges = [], [], []
    cur_page = ""
    for s in steps:
        obs = s.get("observed") or {}
        action = (s.get("action") or "")
        verb = (obs.get("verb") or "").strip().lower()
        label = obs.get("label") or ""
        # infer the page from an Open/navigate action's url
        low = action.lower()
        if low.startswith("open ") or verb == "navigate":
            import re
            m = re.search(r"https?://[^\s']+|/[\w\-./]+", action)
            if m:
                nk = m.group(0)
                if cur_page and nk != cur_page:
                    edges.append(Edge(cur_page, nk))
                cur_page = nk
                if nk not in nodes_seen:
                    nodes_seen.append(nk)
        if label and verb in ("click", "type", "select", "check", "fill"):
            acts.append(Action(verb=verb, target_label=label,
                               value=(obs.get("value") or ""), page_key=cur_page,
                               confidence=(0.9 if s.get("confidence") == "high" else 0.5),
                               automation_ready=(s.get("confidence") == "high"),
                               source="testcase"))
        nu = obs.get("next_url")
        if nu and cur_page and nu != cur_page:
            edges.append(Edge(cur_page, nu))
    nodes = [PageNode(page_key=k) for k in dict.fromkeys(nodes_seen)]
    return CanonicalDoc(page_nodes=nodes, actions=acts, edges=edges)


# ── Ground-truth labels (hand-verified from app knowledge + tonight's review) ──

SAUCE_LABEL = CanonicalDoc(
    page_nodes=[PageNode(page_key=k, visible_in_video=True) for k in
                ["swag labs", "/inventory.html", "/cart.html", "/checkout-step-one.html",
                 "/checkout-step-two.html", "/checkout-complete.html"]],
    actions=[
        Action("type", "Username", "visual_user", "swag labs", visible_in_video=True),
        Action("type", "Password", "", "swag labs", visible_in_video=True),   # masked → no value target
        Action("click", "Login", "", "swag labs", visible_in_video=True),
        Action("click", "Add to cart", "", "/inventory.html", visible_in_video=True),  # Bolt
        Action("click", "Add to cart", "", "/inventory.html", visible_in_video=True),  # Fleece
        Action("click", "Add to cart", "", "/inventory.html", visible_in_video=True),  # Onesie
        Action("click", "Remove", "", "/cart.html", visible_in_video=True),
        Action("click", "Checkout", "", "/cart.html", visible_in_video=True),
        Action("type", "First Name", "test", "/checkout-step-one.html", visible_in_video=True),
        Action("type", "Last Name", "test", "/checkout-step-one.html", visible_in_video=True),
        Action("type", "Zip/Postal Code", "", "/checkout-step-one.html", visible_in_video=True),
        Action("click", "Continue", "", "/checkout-step-one.html", visible_in_video=True),
        Action("click", "Finish", "", "/checkout-step-two.html", visible_in_video=True),
        Action("click", "Back Home", "", "/checkout-complete.html", visible_in_video=True),
    ],
    edges=[Edge("swag labs", "/inventory.html"), Edge("/inventory.html", "/cart.html"),
           Edge("/cart.html", "/checkout-step-one.html"),
           Edge("/checkout-step-one.html", "/checkout-step-two.html"),
           Edge("/checkout-step-two.html", "/checkout-complete.html"),
           Edge("/checkout-complete.html", "/inventory.html")],
)

AEGIS_LABEL = CanonicalDoc(
    page_nodes=[PageNode(page_key=k, visible_in_video=True) for k in
                ["/apply/profile", "/apply/coverage", "/apply/review"]],
    actions=[
        Action("type", "First name", "REDDY", "/apply/profile", visible_in_video=True),
        Action("type", "Last name", "KARNA", "/apply/profile", visible_in_video=True),
        Action("select", "Country", "Canada", "/apply/profile", visible_in_video=True),
        Action("type", "Phone", "(555) 555-5555", "/apply/profile", visible_in_video=True),
        Action("type", "Date of birth", "03/25/1983", "/apply/profile", visible_in_video=True),
        Action("type", "Tax ID / SSN", "123-45-6789", "/apply/profile", visible_in_video=True),
        Action("type", "State / Province", "Florida", "/apply/profile", visible_in_video=True),
        Action("select", "Employment", "Employed", "/apply/profile", visible_in_video=True),
        Action("click", "Next", "", "/apply/profile", visible_in_video=True),
        Action("select", "Coverage amount", "$950,000", "/apply/coverage", visible_in_video=True),
        Action("click", "Add a beneficiary", "", "/apply/coverage", visible_in_video=True),
        Action("type", "Beneficiary full name", "SECOND TEST", "/apply/coverage", visible_in_video=True),
        Action("click", "I confirm the disclosures above are accurate.", "", "/apply/coverage", visible_in_video=True),
    ],
    edges=[Edge("/apply/profile", "/apply/coverage"), Edge("/apply/coverage", "/apply/review")],
)


def _print(title: str, sc: dict):
    print(f"\n========== {title} ==========")
    f = sc["faithfulness"]
    print(f"  FAITHFULNESS  fabrication_rate={f['rate']}  ({f['fabricated_count']}/{f['confident_count']} confident rows unmatched)")
    if f["examples"]:
        for e in f["examples"]:
            print(f"      fabricated: {e}")
    c = sc["completeness"]["actions"]
    print(f"  COMPLETENESS  action F1={c['f1']}  (P={c['precision']} R={c['recall']}, tp={c['tp']} fp={c['fp']} fn={c['fn']})")
    sd = sc["completeness"]["silent_drops"]
    print(f"  SILENT-DROPS  silent={sd['silent_drops']}  flagged={sd['flagged_misses']}  (visible misses={sd['visible_misses']})")
    if sd["missed_examples"]:
        print(f"      missed: {sd['missed_examples']}")
    cal = sc["calibration"]
    print(f"  CALIBRATION   ECE={cal['ece']}  overconfident_wrong_mass={cal['overconfident_wrong_mass']}  (n={cal['n']})")
    pg = sc["page_graph"]
    print(f"  PAGE-GRAPH    node F1={pg['node']['f1']} edge F1={pg['edge']['f1']} GED={pg['graph_edit_distance']}")
    if pg["fabricated_edges"]:
        print(f"      fabricated edges: {pg['fabricated_edges']}")
    vs = sc["value_survival"]
    print(f"  VALUE-SURVIVAL value_recall={vs['value_recall']}  ({vs['survived']}/{vs['targets']})")
    if vs["dropped"]:
        print(f"      dropped values: {vs['dropped']}")


if __name__ == "__main__":
    print("#" * 64)
    print("#  CANONICAL TRUST BASELINE — FIRST MEASURED RUN (real prod data)")
    print("#" * 64)
    cards = []
    sc1 = score(extraction_doc(SAUCE), SAUCE_LABEL)
    _print("saucedemo · EXTRACTION (page_actions) vs ground truth", sc1); cards.append(sc1)
    sc2 = score(testcase_doc(SAUCE), SAUCE_LABEL)
    _print("saucedemo · TEST CASE (generated) vs ground truth", sc2); cards.append(sc2)
    sc3 = score(extraction_doc(AEGIS), AEGIS_LABEL)
    _print("aegis insurance · EXTRACTION vs ground truth", sc3); cards.append(sc3)
    print("\n========== AGGREGATE (3 scorecards) ==========")
    print(json.dumps(aggregate(cards), indent=2))
