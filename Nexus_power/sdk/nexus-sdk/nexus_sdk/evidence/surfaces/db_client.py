"""DB-client surface extractor.

Recognises SQL Developer, DBeaver, TOAD, SSMS, MySQL Workbench, pgAdmin,
DataGrip — any tool whose screen layout is "SQL editor pane + result
grid".  These tools share a visual signature that's hard to mistake:

  * A line starting with a SQL keyword (``SELECT``, ``INSERT``,
    ``UPDATE``, ``DELETE``, ``CREATE``, ``ALTER``, ``WITH``) — the
    query the user is composing or has run.
  * A *result grid* below the editor: one header row of column names
    followed by data rows.  OCR sees this as a row of single-word
    tokens followed by rows of values separated by 2+ spaces.
  * A *connection chip* in the top toolbar showing the active server
    (``prod-db / sales_schema`` or ``localhost:5432 / postgres``).

We emit three control kinds:

  * ``db_query``         — the SQL the user typed (action_kind=enter_text)
  * ``db_result_column`` — a column header in the result grid
                            (action_kind=enter_text, automation_ready=False —
                             this is read-only evidence)
  * ``db_connection``    — the active connection chip
                            (action_kind=select_option)

Selectors use ``db://`` URIs that downstream automation can consume:

    db://query?id=<deterministic hash>
    db://result_grid?column=customer_name
    db://connection?server=prod-db
"""
from __future__ import annotations

import hashlib
import re
import uuid

from .base import SurfaceExtractor, register_surface


_NS = uuid.NAMESPACE_OID

# Strong markers that this is a DB client UI, not a code editor or
# documentation page that happens to mention SQL.
_DB_CLIENT_CONFIRM_TOKENS = (
    "dbeaver", "datagrip", "toad", "sql developer", "ssms",
    "mysql workbench", "pgadmin", "sqlplus", "snowflake",
    "query result", "query results", "result set", "execution time",
    "rows affected", "rows returned", "execution plan",
    "schema browser", "object browser", "session", "transaction log",
    "schema:", "database:", "connection:", "host:",
)

# Match a SQL statement at the start of any line.  We accept the common
# top-level keywords; we deliberately don't try to parse the full
# query because OCR mangles punctuation — the goal is "is there a
# query here, and what's its fingerprint".
_SQL_START_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE|MERGE|WITH|EXPLAIN|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Connection chip in the toolbar: ``prod-db / sales`` or ``localhost:5432``
_CONN_RE = re.compile(
    r"\b(host|server|database|schema|connection)\s*[:=]\s*([A-Za-z0-9_\-./:]+)",
    re.IGNORECASE,
)

# A plausible result-grid header line: 3+ short identifier-like tokens
# separated by 2+ spaces.  Headers don't end with punctuation and don't
# contain SQL keywords (those belong to the query line above).
_GRID_HEADER_HINT_RE = re.compile(r"\b[a-z][a-z0-9_]{1,40}\b")


def _make_id(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "db_client:" + ":".join(parts)))


def _query_fingerprint(query_text: str) -> str:
    """Stable 12-char fingerprint of a SQL query.

    Used as part of the deterministic ``control_id`` so two captures of
    the same query collide on the same row.
    """
    norm = re.sub(r"\s+", " ", query_text.strip().lower())
    return hashlib.sha1(norm.encode("utf-8", "ignore")).hexdigest()[:12]


def _extract_query_block(ocr_text: str, sql_match: re.Match) -> str:
    """From the OCR text, extract the line containing the SQL keyword
    and any continuation lines (indented or starting with FROM/WHERE/JOIN).
    """
    start_line = ocr_text.rfind("\n", 0, sql_match.start()) + 1
    lines = ocr_text[start_line:].splitlines()
    if not lines:
        return ""
    out: list[str] = [lines[0]]
    continuation = re.compile(
        r"^\s*(FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|GROUP|ORDER|HAVING|LIMIT|UNION|ON|AND|OR|SET|VALUES)\b",
        re.IGNORECASE,
    )
    for ln in lines[1:6]:  # up to 5 continuation lines
        if not ln.strip():
            break
        if ln.startswith(" ") or ln.startswith("\t") or continuation.match(ln):
            out.append(ln.strip())
        else:
            break
    return " ".join(out)[:500]


def _detect_grid_columns(ocr_text: str, query_text: str) -> list[str]:
    """Find a likely result-grid header row.

    The heuristic: take the first line AFTER the query that contains 3+
    short alphanumeric tokens separated by 2+ spaces and is not itself a
    SQL fragment.
    """
    if not query_text:
        return []
    after_query = ocr_text[ocr_text.find(query_text) + len(query_text):]
    for ln in after_query.splitlines():
        stripped = ln.strip()
        if not stripped or _SQL_START_RE.search(stripped):
            continue
        # Split on runs of 2+ spaces — typical grid column separator.
        cols = [c.strip() for c in re.split(r"\s{2,}", stripped) if c.strip()]
        # Require at least 3 columns and that every column looks like an
        # identifier (no spaces, no punctuation).
        if len(cols) >= 3 and all(
            _GRID_HEADER_HINT_RE.fullmatch(c.lower()) for c in cols
        ):
            return cols[:20]
    return []


class DBClientExtractor(SurfaceExtractor):
    """Extract controls from SQL editor + result-grid surfaces."""

    NAME = "db_client"
    APP_TYPE_TOKENS = (
        "database", "db_client", "sql", "rdbms", "data_grip",
        "dbeaver", "toad", "ssms", "pgadmin", "snowflake",
    )

    def extract(
        self,
        scene: dict,
        frame: dict,
        artifact_id: str = "",
        tenant_id: str = "",
        all_frames: list | None = None,
    ) -> list[dict]:
        scene_id = scene.get("scene_id", "")
        frame_id = frame.get("frame_id") or None
        ocr_text = frame.get("extracted_text", "") or ""
        ocr_confidence = float(frame.get("ocr_confidence", 0.0) or 0.0)

        haystack = ocr_text.lower()
        if not any(tok in haystack for tok in _DB_CLIENT_CONFIRM_TOKENS):
            # Either the OCR has no DB-client marker, OR the LLaVA
            # mis-classified the screen.  Don't emit DB selectors on a
            # web page that happens to contain the word "SELECT".
            return []

        sel_conf = round(max(ocr_confidence, 0.6) * 0.85, 4)
        emitted: list[dict] = []

        # ── 1. Active query ─────────────────────────────────────────
        sql_match = _SQL_START_RE.search(ocr_text)
        query_text = _extract_query_block(ocr_text, sql_match) if sql_match else ""
        if query_text:
            fp = _query_fingerprint(query_text)
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "query", fp),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "db_query",
                "label_text": "SQL Query",
                "value_text": query_text,
                "action_kind": "enter_text",
                "observed_value": query_text,
                "display_label": f"Run: {query_text[:80]}",
                "bounding_box": {},
                "selector_source": "db_client",
                "playwright_selector": f"db://query?id={fp}",
                "selector_confidence": sel_conf,
                "automation_ready": True,
            })

        # ── 2. Result-grid column headers ───────────────────────────
        columns = _detect_grid_columns(ocr_text, query_text)
        for col in columns:
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "col", col),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "db_result_column",
                "label_text": col,
                "value_text": "",
                "action_kind": "enter_text",
                "observed_value": "",
                "display_label": f"Column: {col}",
                "bounding_box": {},
                "selector_source": "db_client",
                "playwright_selector": f"db://result_grid?column={col}",
                "selector_confidence": sel_conf,
                # Result columns are read-only assertions, not typeable.
                "automation_ready": False,
            })

        # ── 3. Connection chip ──────────────────────────────────────
        conn_seen: set[str] = set()
        for m in _CONN_RE.finditer(ocr_text):
            kind = m.group(1).lower()
            value = m.group(2).strip()
            if not value or value.lower() in conn_seen:
                continue
            conn_seen.add(value.lower())
            emitted.append({
                "control_id": _make_id(artifact_id, scene_id, "conn", f"{kind}.{value}"),
                "scene_id": scene_id,
                "frame_id": frame_id,
                "artifact_id": artifact_id,
                "tenant_id": tenant_id,
                "element_type": "db_connection",
                "label_text": kind.title(),
                "value_text": value,
                "action_kind": "select_option",
                "observed_value": value,
                "display_label": f"Connect: {kind}={value}",
                "bounding_box": {},
                "selector_source": "db_client",
                "playwright_selector": f"db://connection?{kind}={value}",
                "selector_confidence": sel_conf,
                "automation_ready": True,
            })
            if len(conn_seen) >= 4:
                break

        return emitted


register_surface(DBClientExtractor())
