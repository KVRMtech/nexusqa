"""
QA Execution Target Extension for Legs Engine.

Extracted from engines/legs-engine/main.py TargetType enum and execution capabilities.
Defines execution targets (Web UI, API, Database, Mainframe) with their
capabilities, required configuration, and self-healing strategies.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import ExecutionExtension, ExecutionTargetDefinition


def build_execution_extension() -> ExecutionExtension:
    """Build the QA execution extension for Legs engine."""
    return ExecutionExtension(
        domain="insurance",
        target_types=[
            ExecutionTargetDefinition(
                name="web_ui",
                display_name="Web UI (Playwright)",
                description=(
                    "Browser-based UI testing via Playwright. Supports Chromium, "
                    "Firefox, WebKit. Full screenshot/video evidence capture."
                ),
                capabilities=[
                    "click", "fill", "select", "navigate", "screenshot",
                    "video_record", "file_upload", "iframe_support",
                    "multi_tab", "network_interception", "har_capture",
                    "accessibility_audit", "visual_regression",
                ],
                required_config=["base_url"],
                default_timeout_ms=30000,
            ),
            ExecutionTargetDefinition(
                name="api",
                display_name="REST/SOAP API",
                description=(
                    "API testing via httpx. Supports REST and SOAP. "
                    "Validates response codes, schemas, and business logic."
                ),
                capabilities=[
                    "get", "post", "put", "patch", "delete",
                    "multipart_upload", "json_schema_validation",
                    "response_time_assertion", "header_validation",
                    "certificate_auth", "oauth2", "soap_envelope",
                ],
                required_config=["base_url"],
                default_timeout_ms=30000,
            ),
            ExecutionTargetDefinition(
                name="database",
                display_name="Database Validation",
                description=(
                    "Direct database queries for data validation. "
                    "Supports Oracle, SQL Server, PostgreSQL, MySQL."
                ),
                capabilities=[
                    "select_query", "row_count_assertion",
                    "value_comparison", "referential_integrity_check",
                    "data_snapshot", "before_after_comparison",
                    "stored_procedure_execution",
                ],
                required_config=["connection_string", "db_type"],
                default_timeout_ms=60000,
            ),
            ExecutionTargetDefinition(
                name="mainframe",
                display_name="Mainframe (TN3270)",
                description=(
                    "Mainframe terminal testing via TN3270 emulation. "
                    "For legacy insurance admin systems on IBM z/OS."
                ),
                capabilities=[
                    "connect", "send_keys", "read_screen",
                    "wait_for_field", "tab_navigate",
                    "pf_key", "screen_capture",
                    "field_validation", "transaction_submit",
                ],
                required_config=["host", "port"],
                default_timeout_ms=60000,
            ),
        ],
        self_healing_strategies=[
            "selector_fallback",
            "text_content_matching",
            "aria_label_matching",
            "visual_element_detection",
            "dom_structure_analysis",
            "retry_with_wait",
            "page_reload_retry",
            "alternative_path_discovery",
        ],
        evidence_types=[
            "screenshot",
            "video",
            "har_trace",
            "console_log",
            "network_log",
            "dom_snapshot",
            "accessibility_report",
            "performance_metrics",
        ],
    )
