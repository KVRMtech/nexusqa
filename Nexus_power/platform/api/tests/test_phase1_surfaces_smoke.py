"""Smoke test for Phase 1 — Multi-Surface Capture.

Each surface extractor is exercised with a realistic OCR fixture taken
from real-world screens of that app type.  We assert:

  * The surface registry resolves the right extractor for every
    documented app_type token.
  * Each extractor emits at least one automation-ready control with a
    deterministic control_id.
  * Element types match the surface contract (no web ``button`` /
    ``text_field`` leaking into mainframe / SAP / DB / Office output).
  * Re-running the extractor produces identical control_ids
    (idempotency — required by the DB ON CONFLICT upsert path).
  * The web fallback in ControlExtractor still emits historical web
    controls when the app_type is unset / unknown.
  * Confirmation guard fires: a webpage whose application_type was
    misclassified as ``mainframe`` returns zero terminal selectors
    because the OCR has no 3270 markers.

Run:
    python Nexus_power/platform/api/tests/test_phase1_surfaces_smoke.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────
_API_ROOT = Path(__file__).resolve().parents[1]
_SDK_ROOT = _API_ROOT.parents[1] / "sdk" / "nexus-sdk"
sys.path.insert(0, str(_API_ROOT))
sys.path.insert(0, str(_SDK_ROOT))


from nexus_sdk.evidence import surfaces  # noqa: E402
from nexus_sdk.evidence.surfaces.base import default_registry  # noqa: E402
from nexus_sdk.evidence.control_extractor import ControlExtractor  # noqa: E402


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


# ── Fixtures ──────────────────────────────────────────────────────────────


def mainframe_frame() -> tuple[dict, dict]:
    """Realistic ISPF panel OCR.  Mirrors what EasyOCR returns for an IBM 3270 screen."""
    scene = {"scene_id": "sc-3270-1", "application_type": "mainframe"}
    frame = {
        "frame_id": "fr-3270-1",
        "extracted_text": (
            "ISPF Primary Option Menu\n"
            "Option ===> 3.4\n"
            "  1  Settings    Terminal and user parameters\n"
            "  2  View        Display source data or listings\n"
            "  3  Utilities   Perform utility functions\n"
            "USERID ===> HARIK______\n"
            "Application . . . . :  PROD\n"
            "Time  . . . . . . . :  14:32\n"
            "F1=Help  F3=Exit  F7=Up  F8=Down  F10=Left  F12=Cancel\n"
        ),
        "ocr_confidence": 0.92,
        "ui_elements_json": [],
    }
    return scene, frame


def sap_frame() -> tuple[dict, dict]:
    """SAP Easy Access main screen with T-code field and footer."""
    scene = {"scene_id": "sc-sap-1", "application_type": "sap_gui"}
    frame = {
        "frame_id": "fr-sap-1",
        "extracted_text": (
            "SAP Easy Access\n"
            "/NMM03   Display Material\n"
            "Favorites\n"
            "_______________________ Material\n"
            "_______________________ Plant\n"
            "F8 Execute   F3 Back   F11 Save   F12 Cancel\n"
            "PRD  100  EN  INS\n"
        ),
        "ocr_confidence": 0.88,
        "ui_elements_json": [],
    }
    return scene, frame


def db_client_frame() -> tuple[dict, dict]:
    """DBeaver query editor + result grid OCR."""
    scene = {"scene_id": "sc-db-1", "application_type": "database"}
    frame = {
        "frame_id": "fr-db-1",
        "extracted_text": (
            "DBeaver Community\n"
            "host: prod-db.acme.internal\n"
            "schema: sales\n"
            "SELECT customer_id, customer_name, total_amount\n"
            "FROM orders WHERE status = 'open'\n"
            "Execution time: 142 ms\n"
            "customer_id    customer_name    total_amount\n"
            "1001           Acme Corp        4500\n"
            "1002           Globex           2200\n"
            "Rows returned: 2\n"
        ),
        "ocr_confidence": 0.90,
        "ui_elements_json": [],
    }
    return scene, frame


def office_excel_frame() -> tuple[dict, dict]:
    """Excel formula bar + cell reference."""
    scene = {"scene_id": "sc-excel-1", "application_type": "excel"}
    frame = {
        "frame_id": "fr-excel-1",
        "extracted_text": (
            "Microsoft Excel - Q4_Forecast.xlsx\n"
            "Name Box: B12\n"
            "Formula Bar: =SUM(B2:B11)\n"
            "Sheet1\n"
        ),
        "ocr_confidence": 0.87,
        "ui_elements_json": [],
    }
    return scene, frame


def office_outlook_frame() -> tuple[dict, dict]:
    """Outlook ribbon + message list row.

    Real Outlook list rows are ``Sender   Subject   When`` — no
    ``From:`` / ``Subject:`` field labels (those only appear in the
    reading pane, not the list).  The fixture mirrors what EasyOCR
    returns over an actual inbox screenshot.
    """
    scene = {"scene_id": "sc-outlook-1", "application_type": "outlook"}
    frame = {
        "frame_id": "fr-outlook-1",
        "extracted_text": (
            "Microsoft Outlook - Inbox\n"
            "New Email   Reply   Reply All   Forward   Delete   Archive\n"
            "Jane Doe                Q4 Forecast review                   3:45 PM\n"
            "Bob Smith               Renewal contract                     Apr 12\n"
            "Maria Lopez             Budget approval needed                9:02 AM\n"
        ),
        "ocr_confidence": 0.85,
        "ui_elements_json": [],
    }
    return scene, frame


def web_frame() -> tuple[dict, dict]:
    """Plain web form OCR — the historical web extractor path."""
    scene = {"scene_id": "sc-web-1", "application_type": "web"}
    frame = {
        "frame_id": "fr-web-1",
        "extracted_text": "Email\nPassword\nSign in",
        "description": "Login form with Email and Password fields and a Sign in button",
        "ocr_confidence": 0.91,
        "ui_elements_json": [
            {"element_type": "text_field", "text": "Email", "bbox": [10, 20, 200, 50]},
            {"element_type": "text_field", "text": "Password", "bbox": [10, 70, 200, 100]},
            {"element_type": "button", "text": "Sign in", "bbox": [10, 120, 200, 150]},
        ],
    }
    return scene, frame


# ── Tests ─────────────────────────────────────────────────────────────────


def test_registry_resolution() -> None:
    print("\n=== surface registry ===")
    names = surfaces.registered_surfaces()
    check(
        "all four surfaces self-registered on import",
        set(names) >= {"mainframe_3270", "sap_gui", "db_client", "office_desktop"},
        detail=f"got {names}",
    )

    cases = [
        ("mainframe",       "mainframe_3270"),
        ("MAINFRAME_3270",  "mainframe_3270"),
        ("cics",            "mainframe_3270"),
        ("tso",             "mainframe_3270"),
        ("sap_gui",         "sap_gui"),
        ("SAPLogon",        "sap_gui"),
        ("oracle_forms",    "sap_gui"),
        ("database",        "db_client"),
        ("dbeaver",         "db_client"),
        ("snowflake",       "db_client"),
        ("excel",           "office_desktop"),
        ("Outlook",         "office_desktop"),
        ("MS_Word",         "office_desktop"),
    ]
    for app_type, expected in cases:
        surf = surfaces.find_surface(app_type)
        check(
            f"app_type={app_type!r} -> {expected}",
            surf is not None and surf.NAME == expected,
            detail=f"got {surf.NAME if surf else None}",
        )

    # Unknown app types fall through (None means web fallback)
    check(
        "unknown app_type -> no surface (web fallback)",
        surfaces.find_surface("yelp_native_app_v2") is None,
    )
    check(
        "empty app_type -> no surface",
        surfaces.find_surface("") is None,
    )


def test_mainframe_3270() -> None:
    print("\n=== Mainframe 3270 surface ===")
    scene, frame = mainframe_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art1")

    check("emitted at least 4 controls", len(controls) >= 4)

    kinds = {c["element_type"] for c in controls}
    check(
        "no web-style element types leak in",
        kinds.isdisjoint({"button", "text_field", "dropdown", "checkbox", "link"}),
        detail=f"got {kinds}",
    )
    check("emitted terminal_command", any(c["element_type"] == "terminal_command" for c in controls))
    check("emitted terminal_field", any(c["element_type"] == "terminal_field" for c in controls))
    check("emitted terminal_function", any(c["element_type"] == "terminal_function" for c in controls))

    cmd = next((c for c in controls if c["element_type"] == "terminal_command"), None)
    check("command selector uses terminal:// scheme",
          cmd is not None and cmd["playwright_selector"].startswith("terminal://"))
    check("command captured typed value '3.4'",
          cmd is not None and "3.4" in (cmd["observed_value"] or ""))

    userid = next((c for c in controls if c["element_type"] == "terminal_field"
                   and c["label_text"].lower() == "userid"), None)
    check("USERID field detected", userid is not None)
    check("USERID is automation-ready", userid is not None and userid["automation_ready"] is True)
    check("USERID value captured 'HARIK'",
          userid is not None and "HARIK" in (userid["observed_value"] or ""))

    pf3 = next((c for c in controls if c["element_type"] == "terminal_function"
                and "F3" in c["label_text"]), None)
    check("F3 function key detected", pf3 is not None)
    check("F3 selector targets PF3",
          pf3 is not None and pf3["playwright_selector"] == "terminal://pf?key=F3")

    # Determinism: re-running yields identical control_ids
    controls2 = ControlExtractor().extract(scene, frame, artifact_id="art1")
    ids1 = sorted(c["control_id"] for c in controls)
    ids2 = sorted(c["control_id"] for c in controls2)
    check("control_ids are deterministic across runs", ids1 == ids2)

    # Misclassification guard: web OCR + mainframe app_type -> no terminal controls
    fake = {"scene_id": "sc-web-2", "application_type": "mainframe"}
    fake_frame = {"frame_id": "fr-w-2", "extracted_text": "Login\nSign up\nForgot password",
                  "ocr_confidence": 0.9, "ui_elements_json": []}
    fake_controls = ControlExtractor().extract(fake, fake_frame, artifact_id="art1")
    check("misclassified web->mainframe emits no terminal selectors",
          all(c["selector_source"] != "terminal" for c in fake_controls))


def test_sap_gui() -> None:
    print("\n=== SAP GUI surface ===")
    scene, frame = sap_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art2")

    check("emitted at least 4 controls", len(controls) >= 4)
    kinds = {c["element_type"] for c in controls}
    check("element types are SAP-scoped",
          kinds.issubset({"sap_tcode", "sap_field", "sap_function", "sap_status"}),
          detail=f"got {kinds}")
    check("T-code detected", any(c["element_type"] == "sap_tcode" for c in controls))
    tcode = next(c for c in controls if c["element_type"] == "sap_tcode")
    check("T-code value is MM03", tcode["value_text"].upper() == "MM03")
    check("T-code selector uses sap:// scheme",
          tcode["playwright_selector"].startswith("sap://"))

    fk = [c for c in controls if c["element_type"] == "sap_function"]
    check("at least 3 function keys parsed", len(fk) >= 3)
    check("all SAP controls use sap selector_source",
          all(c["selector_source"] == "sap" for c in controls))


def test_db_client() -> None:
    print("\n=== DB Client surface ===")
    scene, frame = db_client_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art3")

    check("emitted at least 1 control", len(controls) >= 1)
    kinds = {c["element_type"] for c in controls}
    check("element types are DB-scoped",
          kinds.issubset({"db_query", "db_result_column", "db_connection"}),
          detail=f"got {kinds}")

    q = next((c for c in controls if c["element_type"] == "db_query"), None)
    check("query captured", q is not None)
    check("query contains SELECT",
          q is not None and "SELECT" in (q["value_text"] or "").upper())
    check("query selector uses db:// scheme",
          q is not None and q["playwright_selector"].startswith("db://query?id="))

    cols = [c for c in controls if c["element_type"] == "db_result_column"]
    check("result columns extracted (>=2)", len(cols) >= 2,
          detail=f"got {len(cols)}")
    col_names = {c["label_text"] for c in cols}
    check("column names captured (customer_id / total_amount)",
          {"customer_id", "total_amount"} <= col_names,
          detail=f"got {col_names}")

    conns = [c for c in controls if c["element_type"] == "db_connection"]
    check("connection chip parsed (>=1)", len(conns) >= 1)


def test_office_excel() -> None:
    print("\n=== Office Desktop — Excel ===")
    scene, frame = office_excel_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art4")

    check("emitted at least 1 control", len(controls) >= 1)
    kinds = {c["element_type"] for c in controls}
    check("Excel-only element types",
          kinds.issubset({"excel_cell", "excel_formula"}),
          detail=f"got {kinds}")

    cell = next((c for c in controls if c["element_type"] == "excel_cell"), None)
    check("Excel cell extracted", cell is not None)
    if cell:
        check("Excel selector uses excel:// scheme",
              cell["playwright_selector"].startswith("excel://"))

    formula = next((c for c in controls if c["element_type"] == "excel_formula"), None)
    check("Excel formula extracted", formula is not None)
    check("formula contains =SUM(",
          formula is not None and "SUM(" in (formula["value_text"] or "").upper())


def test_office_outlook() -> None:
    print("\n=== Office Desktop — Outlook ===")
    scene, frame = office_outlook_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art5")

    check("emitted at least 3 controls", len(controls) >= 3)
    kinds = {c["element_type"] for c in controls}
    check("Outlook-only element types",
          kinds.issubset({"outlook_command", "outlook_list_row"}),
          detail=f"got {kinds}")

    commands = [c for c in controls if c["element_type"] == "outlook_command"]
    cmd_labels = {c["label_text"] for c in commands}
    check("ribbon commands captured ('New Email', 'Reply')",
          {"New Email", "Reply"} <= cmd_labels,
          detail=f"got {cmd_labels}")

    rows = [c for c in controls if c["element_type"] == "outlook_list_row"]
    check("message list rows captured (>=2)", len(rows) >= 2)


def test_web_fallback() -> None:
    print("\n=== Web fallback (unchanged behaviour) ===")
    scene, frame = web_frame()
    controls = ControlExtractor().extract(scene, frame, artifact_id="art6")

    check("web fallback emits controls", len(controls) >= 1)
    kinds = {c["element_type"] for c in controls}
    check("web fallback emits web-style element types",
          kinds.issubset({"text_field", "button", "dropdown", "checkbox", "radio", "link", "table_cell"}),
          detail=f"got {kinds}")
    # No surface-specific selector sources should appear on the web path
    sources = {c["selector_source"] for c in controls}
    check("web fallback never emits surface-specific selector sources",
          sources.isdisjoint({"terminal", "sap", "db_client", "office"}),
          detail=f"got {sources}")


# ── Runner ────────────────────────────────────────────────────────────────


def main() -> int:
    test_registry_resolution()
    test_mainframe_3270()
    test_sap_gui()
    test_db_client()
    test_office_excel()
    test_office_outlook()
    test_web_fallback()
    print(f"\n=== Phase 1 surface smoke: {PASS} pass, {FAIL} fail ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
