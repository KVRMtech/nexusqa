"""
Insurance Knowledge Graph Schema Extension for Backbone Engine.

Extracted from engines/backbone-engine/main.py NodeType and RelationType enums.
Defines insurance-specific node types and relationship types for the knowledge graph.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import (
    GraphSchemaExtension,
    NodeTypeDefinition,
    RelationshipTypeDefinition,
)


def build_graph_schema_extension() -> GraphSchemaExtension:
    """Build the insurance graph schema extension for Backbone engine."""
    return GraphSchemaExtension(
        domain="insurance",
        node_types=[
            # ── Domain Knowledge Nodes ──────────────────────────
            NodeTypeDefinition(
                name="Product",
                display_name="Insurance Product",
                description="An insurance product (Term, Whole Life, UL, etc.)",
                properties={
                    "product_code": "str",
                    "product_name": "str",
                    "product_type": "str",
                    "effective_date": "datetime",
                    "states_approved": "list[str]",
                },
                required_properties=["product_code", "product_name"],
                icon="shield",
                color="#3B82F6",
            ),
            NodeTypeDefinition(
                name="Coverage",
                display_name="Coverage / Benefit",
                description="A coverage or benefit within an insurance product",
                properties={
                    "coverage_code": "str",
                    "coverage_name": "str",
                    "min_amount": "float",
                    "max_amount": "float",
                    "rider_eligible": "bool",
                },
                required_properties=["coverage_code", "coverage_name"],
                icon="umbrella",
                color="#10B981",
            ),
            NodeTypeDefinition(
                name="RateTable",
                display_name="Rate Table",
                description="Premium rate table (age × gender × tobacco × class → rate)",
                properties={
                    "table_id": "str",
                    "product_code": "str",
                    "effective_date": "datetime",
                    "rate_basis": "str",
                    "jurisdiction": "str",
                },
                required_properties=["table_id", "product_code"],
                icon="table",
                color="#F59E0B",
            ),
            NodeTypeDefinition(
                name="Form",
                display_name="Insurance Form",
                description="Regulatory form (application, policy form, endorsement)",
                properties={
                    "form_number": "str",
                    "form_name": "str",
                    "form_type": "str",
                    "edition_date": "str",
                    "state_variations": "list[str]",
                },
                required_properties=["form_number", "form_name"],
                icon="file-text",
                color="#8B5CF6",
            ),
            NodeTypeDefinition(
                name="StateRegulation",
                display_name="State Regulation",
                description="State-specific insurance regulation or requirement",
                properties={
                    "state_code": "str",
                    "regulation_id": "str",
                    "regulation_title": "str",
                    "effective_date": "datetime",
                    "department": "str",
                },
                required_properties=["state_code", "regulation_id"],
                icon="landmark",
                color="#EF4444",
            ),
            NodeTypeDefinition(
                name="Filing",
                display_name="Regulatory Filing",
                description="State filing for product/rate approval",
                properties={
                    "filing_id": "str",
                    "serff_tracking": "str",
                    "filing_type": "str",
                    "status": "str",
                    "submission_date": "datetime",
                    "approval_date": "datetime",
                    "state_code": "str",
                },
                required_properties=["filing_id", "state_code"],
                icon="inbox",
                color="#F97316",
            ),
            NodeTypeDefinition(
                name="Endorsement",
                display_name="Policy Endorsement",
                description="Endorsement or rider modifying a policy form",
                properties={
                    "endorsement_code": "str",
                    "endorsement_name": "str",
                    "applies_to": "list[str]",
                    "effective_date": "datetime",
                },
                required_properties=["endorsement_code", "endorsement_name"],
                icon="file-plus",
                color="#06B6D4",
            ),
        ],
        relationship_types=[
            # ── Business Domain Relationships ───────────────────
            RelationshipTypeDefinition(
                name="HAS_RULE",
                display_name="Has Business Rule",
                description="Entity contains or is governed by a business rule",
                from_node_types=["Product", "Coverage", "Form", "StateRegulation"],
                to_node_types=["BusinessRule"],
            ),
            RelationshipTypeDefinition(
                name="COVERS",
                display_name="Covers / Provides",
                description="Product provides a coverage or benefit",
                from_node_types=["Product"],
                to_node_types=["Coverage"],
            ),
            RelationshipTypeDefinition(
                name="USES_RATE",
                display_name="Uses Rate Table",
                description="Product or coverage priced using a rate table",
                from_node_types=["Product", "Coverage"],
                to_node_types=["RateTable"],
            ),
            RelationshipTypeDefinition(
                name="REQUIRES_FORM",
                display_name="Requires Form",
                description="Product or regulation requires a specific form",
                from_node_types=["Product", "StateRegulation", "Filing"],
                to_node_types=["Form"],
            ),
            RelationshipTypeDefinition(
                name="REGULATES",
                display_name="Regulates",
                description="State regulation governs a product, form, or rate table",
                from_node_types=["StateRegulation"],
                to_node_types=["Product", "Coverage", "RateTable", "Form"],
            ),
            RelationshipTypeDefinition(
                name="FILED_IN",
                display_name="Filed In State",
                description="Product or form filed in a specific state",
                from_node_types=["Product", "Form"],
                to_node_types=["Filing"],
            ),
            RelationshipTypeDefinition(
                name="ENDORSES",
                display_name="Endorses / Modifies",
                description="Endorsement modifies a policy form or product",
                from_node_types=["Endorsement"],
                to_node_types=["Form", "Product"],
            ),
        ],
        constraints=[
            {
                "type": "unique",
                "node_type": "Product",
                "property": "product_code",
                "description": "Product codes must be unique",
            },
            {
                "type": "unique",
                "node_type": "RateTable",
                "property": "table_id",
                "description": "Rate table IDs must be unique",
            },
            {
                "type": "unique",
                "node_type": "Filing",
                "property": "filing_id",
                "description": "Filing IDs must be unique",
            },
            {
                "type": "unique",
                "node_type": "Form",
                "property": "form_number",
                "description": "Form numbers must be unique",
            },
        ],
    )
