"""QI Engineer Portal — personas, missions, mission_stages, mission_artifacts, mission_messages

Revision ID: 005_qi_portal
Revises: 004_media_processing
Create Date: 2026-03-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005_qi_portal"
down_revision: Union[str, None] = "004_media_processing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Personas ───────────────────────────────────────────
    op.create_table(
        "personas",
        sa.Column("persona_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("avatar_icon", sa.String(50), server_default="brain"),
        sa.Column("system_prompt", sa.Text, server_default=""),
        sa.Column("capabilities", sa.JSON, server_default="[]"),
        sa.Column("stage_config", sa.JSON, server_default="{}"),
        sa.Column("specialty_domains", sa.JSON, server_default="[]"),
        sa.Column("is_system", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_personas_tenant_active", "personas", ["tenant_id", "is_active"])
    op.create_index("ix_personas_slug", "personas", ["tenant_id", "slug"], unique=True)

    # ── Missions ───────────────────────────────────────────
    op.create_table(
        "missions",
        sa.Column("mission_id", sa.String(64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(64),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "persona_id",
            sa.String(64),
            sa.ForeignKey("personas.persona_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("objective", sa.Text, server_default=""),
        sa.Column("status", sa.String(30), server_default="draft"),
        sa.Column("current_stage", sa.Integer, server_default="1"),
        sa.Column("priority", sa.String(20), server_default="medium"),
        sa.Column("tags", sa.JSON, server_default="[]"),
        sa.Column("context", sa.JSON, server_default="{}"),
        sa.Column("summary", sa.Text, server_default=""),
        sa.Column("progress_pct", sa.Float, server_default="0.0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_missions_tenant_status", "missions", ["tenant_id", "status"])
    op.create_index("ix_missions_tenant_user", "missions", ["tenant_id", "user_id"])
    op.create_index("ix_missions_persona", "missions", ["persona_id"])

    # ── Mission Stages ─────────────────────────────────────
    op.create_table(
        "mission_stages",
        sa.Column("stage_id", sa.String(64), primary_key=True),
        sa.Column(
            "mission_id",
            sa.String(64),
            sa.ForeignKey("missions.mission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage_number", sa.Integer, nullable=False),
        sa.Column("stage_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("inputs", sa.JSON, server_default="{}"),
        sa.Column("outputs", sa.JSON, server_default="{}"),
        sa.Column("engine_calls", sa.JSON, server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float, server_default="0.0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
    )
    op.create_index(
        "ix_mission_stages_mission",
        "mission_stages",
        ["mission_id", "stage_number"],
        unique=True,
    )

    # ── Mission Artifacts ──────────────────────────────────
    op.create_table(
        "mission_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column(
            "mission_id",
            sa.String(64),
            sa.ForeignKey("missions.mission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "stage_id",
            sa.String(64),
            sa.ForeignKey("mission_stages.stage_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(50), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("content_json", sa.JSON, server_default="{}"),
        sa.Column("content_text", sa.Text, server_default=""),
        sa.Column("file_path", sa.String(1000), server_default=""),
        sa.Column("file_size_bytes", sa.Integer, server_default="0"),
        sa.Column("item_count", sa.Integer, server_default="0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_mission_artifacts_mission", "mission_artifacts", ["mission_id"])
    op.create_index("ix_mission_artifacts_stage", "mission_artifacts", ["stage_id"])
    op.create_index(
        "ix_mission_artifacts_type",
        "mission_artifacts",
        ["mission_id", "artifact_type"],
    )

    # ── Mission Messages ───────────────────────────────────
    op.create_table(
        "mission_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column(
            "mission_id",
            sa.String(64),
            sa.ForeignKey("missions.mission_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("stage_number", sa.Integer, server_default="1"),
        sa.Column("content_type", sa.String(30), server_default="text"),
        sa.Column("action_data", sa.JSON, nullable=True),
        sa.Column("token_count", sa.Integer, server_default="0"),
        sa.Column("metadata_json", sa.JSON, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_mission_messages_mission",
        "mission_messages",
        ["mission_id", "created_at"],
    )
    op.create_index(
        "ix_mission_messages_stage",
        "mission_messages",
        ["mission_id", "stage_number"],
    )

    # ── Seed system personas ───────────────────────────────
    personas_table = sa.table(
        "personas",
        sa.column("persona_id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
        sa.column("description", sa.Text),
        sa.column("avatar_icon", sa.String),
        sa.column("system_prompt", sa.Text),
        sa.column("capabilities", sa.JSON),
        sa.column("stage_config", sa.JSON),
        sa.column("specialty_domains", sa.JSON),
        sa.column("is_system", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )

    op.bulk_insert(
        personas_table,
        [
            {
                "persona_id": "persona-qi-analyst",
                "tenant_id": "__system__",
                "name": "QI Test Analyst",
                "slug": "qi-test-analyst",
                "description": (
                    "Full-pipeline quality intelligence analyst. Guides you through all 5 stages — "
                    "from knowledge capture through test validation — ensuring comprehensive coverage "
                    "at every step."
                ),
                "avatar_icon": "brain",
                "system_prompt": (
                    "You are an expert Quality Intelligence Test Analyst. Your mission is to guide "
                    "the user through a systematic 5-stage quality assurance process: Capture domain "
                    "knowledge, Understand business rules and relationships, Strategize optimal test "
                    "approaches, Generate comprehensive test artifacts, and Validate results against "
                    "requirements. Be specific, data-driven, and always cite which engine produced "
                    "each insight. Ask clarifying questions when objectives are ambiguous."
                ),
                "capabilities": [
                    "rule_extraction", "test_generation", "knowledge_graph",
                    "contradiction_detection", "compliance_check", "data_generation",
                    "report_generation", "test_execution",
                ],
                "stage_config": {
                    "1_capture": {"engines": ["ears", "eyes", "spine", "shield"], "auto_advance": False},
                    "2_understand": {"engines": ["heart", "backbone", "nerves"], "auto_advance": False},
                    "3_strategize": {"engines": ["heart", "nerves"], "auto_advance": False},
                    "4_generate": {"engines": ["legs", "hands", "mouth"], "auto_advance": False},
                    "5_validate": {"engines": ["legs", "nerves"], "auto_advance": False},
                },
                "specialty_domains": [],
                "is_system": True,
                "is_active": True,
                "sort_order": 1,
            },
            {
                "persona_id": "persona-compliance-auditor",
                "tenant_id": "__system__",
                "name": "Compliance Auditor",
                "slug": "compliance-auditor",
                "description": (
                    "Regulatory compliance specialist focused on ensuring your test coverage "
                    "meets jurisdictional requirements. Excels at gap analysis, audit trail "
                    "generation, and compliance reporting."
                ),
                "avatar_icon": "shield-check",
                "system_prompt": (
                    "You are an expert Compliance Auditor AI. Focus on regulatory compliance, "
                    "gap analysis between requirements and test coverage, jurisdictional rules, "
                    "and audit trail completeness. When analyzing captured knowledge, prioritize "
                    "regulatory items and flag non-compliance risks. Always reference specific "
                    "regulation codes and suggest remediation steps."
                ),
                "capabilities": [
                    "rule_extraction", "compliance_check", "contradiction_detection",
                    "report_generation",
                ],
                "stage_config": {
                    "1_capture": {"engines": ["spine", "shield"], "auto_advance": False},
                    "2_understand": {"engines": ["heart", "backbone"], "auto_advance": False},
                    "3_strategize": {"engines": ["nerves"], "auto_advance": False},
                    "4_generate": {"engines": ["mouth"], "auto_advance": False},
                    "5_validate": {"engines": ["nerves"], "auto_advance": False},
                },
                "specialty_domains": ["compliance", "regulatory", "audit"],
                "is_system": True,
                "is_active": True,
                "sort_order": 2,
            },
            {
                "persona_id": "persona-test-architect",
                "tenant_id": "__system__",
                "name": "Test Architect",
                "slug": "test-architect",
                "description": (
                    "Test design specialist who excels at creating optimal test strategies, "
                    "maximizing coverage with minimal test cases, and designing data-driven "
                    "test frameworks."
                ),
                "avatar_icon": "flask-conical",
                "system_prompt": (
                    "You are an expert Test Architect AI. Your strength is designing optimal test "
                    "strategies — boundary value analysis, equivalence partitioning, pairwise testing, "
                    "and risk-based prioritization. When you receive business rules, focus on "
                    "maximizing coverage while minimizing redundancy. Explain your test design "
                    "rationale and suggest both positive and negative test scenarios."
                ),
                "capabilities": [
                    "rule_extraction", "test_generation", "data_generation",
                    "knowledge_graph",
                ],
                "stage_config": {
                    "1_capture": {"engines": ["spine", "ears"], "auto_advance": False},
                    "2_understand": {"engines": ["heart", "backbone"], "auto_advance": False},
                    "3_strategize": {"engines": ["heart", "nerves"], "auto_advance": False},
                    "4_generate": {"engines": ["legs", "hands"], "auto_advance": False},
                    "5_validate": {"engines": ["legs"], "auto_advance": False},
                },
                "specialty_domains": ["testing", "qa", "automation"],
                "is_system": True,
                "is_active": True,
                "sort_order": 3,
            },
            {
                "persona_id": "persona-data-engineer",
                "tenant_id": "__system__",
                "name": "Data Engineer",
                "slug": "data-engineer",
                "description": (
                    "Specialist in test data engineering — profile generation, boundary analysis, "
                    "synthetic data creation, and data-driven test parameterization."
                ),
                "avatar_icon": "database",
                "system_prompt": (
                    "You are an expert Data Engineer AI for test automation. Your focus is on "
                    "creating high-quality synthetic test data: boundary values, edge cases, "
                    "pairwise combinations, regulatory-compliant data profiles, and negative "
                    "test data. When analyzing requirements, identify data fields, constraints, "
                    "dependencies, and generate comprehensive data workbooks."
                ),
                "capabilities": [
                    "data_generation", "rule_extraction",
                ],
                "stage_config": {
                    "1_capture": {"engines": ["spine"], "auto_advance": False},
                    "2_understand": {"engines": ["heart"], "auto_advance": False},
                    "3_strategize": {"engines": ["heart"], "auto_advance": False},
                    "4_generate": {"engines": ["hands"], "auto_advance": False},
                    "5_validate": {"engines": ["nerves"], "auto_advance": False},
                },
                "specialty_domains": ["data", "synthetic", "generation"],
                "is_system": True,
                "is_active": True,
                "sort_order": 4,
            },
            {
                "persona_id": "persona-knowledge-curator",
                "tenant_id": "__system__",
                "name": "Knowledge Curator",
                "slug": "knowledge-curator",
                "description": (
                    "Expert at extracting, organizing, and preserving organizational knowledge. "
                    "Builds comprehensive knowledge graphs, identifies knowledge gaps, and reduces "
                    "bus-factor risks."
                ),
                "avatar_icon": "book-open",
                "system_prompt": (
                    "You are a Knowledge Curator AI. Your mission is to capture, organize, and "
                    "preserve organizational domain knowledge from various sources — meetings, "
                    "documents, videos, and expert interviews. Focus on building a comprehensive "
                    "knowledge graph, identifying knowledge gaps, detecting contradictions between "
                    "sources, and reducing bus-factor risks. Highlight when critical knowledge "
                    "relies on a single source."
                ),
                "capabilities": [
                    "rule_extraction", "knowledge_graph", "contradiction_detection",
                ],
                "stage_config": {
                    "1_capture": {"engines": ["ears", "eyes", "spine", "shield"], "auto_advance": False},
                    "2_understand": {"engines": ["heart", "backbone", "nerves"], "auto_advance": False},
                    "3_strategize": {"engines": ["heart"], "auto_advance": False},
                    "4_generate": {"engines": ["mouth"], "auto_advance": False},
                    "5_validate": {"engines": ["nerves"], "auto_advance": False},
                },
                "specialty_domains": ["knowledge", "domain", "capture"],
                "is_system": True,
                "is_active": True,
                "sort_order": 5,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("mission_messages")
    op.drop_table("mission_artifacts")
    op.drop_table("mission_stages")
    op.drop_table("missions")
    op.drop_table("personas")
