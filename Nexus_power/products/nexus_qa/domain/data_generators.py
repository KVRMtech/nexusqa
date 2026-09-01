"""
Insurance Synthetic Data Generation Extension for Hands Engine.

Extracted from engines/hands-engine/main.py insurance enums, age bands,
face amounts, and regulatory data. Defines insurance-specific data
profiles for synthetic test data generation.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import (
    DataGeneratorExtension,
    DataProfileDefinition,
    FieldDefinition,
    IDPatternDefinition,
)


def build_data_generator_extension() -> DataGeneratorExtension:
    """Build the insurance data generator extension for Hands engine."""
    return DataGeneratorExtension(
        domain="insurance",
        profiles=[
            # ── Life Insurance Applicant Profile ────────────────
            DataProfileDefinition(
                name="life_insurance_applicant",
                display_name="Life Insurance Applicant",
                description="Synthetic applicant profile for life insurance quoting and underwriting",
                fields=[
                    FieldDefinition(
                        name="gender",
                        field_type="enum",
                        description="Applicant biological sex for insurance rating",
                        enum_values=["male", "female", "non_binary"],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="age",
                        field_type="integer",
                        description="Applicant age at application",
                        min_value=0,
                        max_value=99,
                        synthetic_strategy="boundary",
                    ),
                    FieldDefinition(
                        name="tobacco_status",
                        field_type="enum",
                        description="Tobacco usage classification",
                        enum_values=["non_smoker", "smoker", "former_smoker"],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="health_class",
                        field_type="enum",
                        description="Underwriting risk classification",
                        enum_values=[
                            "preferred_plus", "preferred", "standard_plus",
                            "standard", "substandard_a", "substandard_b", "decline",
                        ],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="product_type",
                        field_type="enum",
                        description="Insurance product type being quoted",
                        enum_values=[
                            "term_10", "term_15", "term_20", "term_30",
                            "whole_life", "universal_life",
                            "variable_universal_life", "indexed_universal_life",
                            "final_expense",
                            "annuity_fixed", "annuity_variable", "annuity_indexed",
                        ],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="face_amount",
                        field_type="float",
                        description="Death benefit / coverage face amount",
                        min_value=1_000,
                        max_value=10_000_000,
                        synthetic_strategy="boundary",
                    ),
                    FieldDefinition(
                        name="jurisdiction",
                        field_type="string",
                        description="US state code (2-letter) for regulatory compliance",
                        pattern=r"^[A-Z]{2}$",
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="payment_mode",
                        field_type="enum",
                        description="Premium payment frequency",
                        enum_values=[
                            "annual", "semi_annual", "quarterly",
                            "monthly", "monthly_eft",
                        ],
                        synthetic_strategy="random",
                    ),
                ],
                constraints=[
                    {
                        "name": "juvenile_no_tobacco",
                        "description": "Applicants under 18 cannot have tobacco status",
                        "condition": "age < 18 → tobacco_status = 'non_smoker'",
                    },
                    {
                        "name": "final_expense_age",
                        "description": "Final expense products typically ages 45-85",
                        "condition": "product_type = 'final_expense' → age >= 45 AND age <= 85",
                    },
                    {
                        "name": "final_expense_face",
                        "description": "Final expense face amounts typically $1K-$50K",
                        "condition": "product_type = 'final_expense' → face_amount <= 50000",
                    },
                    {
                        "name": "annuity_no_face_amount",
                        "description": "Annuity products use premium, not face amount",
                        "condition": "product_type STARTS_WITH 'annuity_' → face_amount is premium_amount",
                    },
                ],
                generation_strategies=["random", "boundary", "equivalence", "pairwise", "risk_focused"],
            ),

            # ── Insurance Rider Selection ───────────────────────
            DataProfileDefinition(
                name="rider_selection",
                display_name="Rider / Benefit Selection",
                description="Optional riders and benefits added to a base policy",
                fields=[
                    FieldDefinition(
                        name="rider_type",
                        field_type="enum",
                        description="Type of rider being added",
                        enum_values=[
                            "wop", "adb", "gpo", "cir", "cola",
                            "child_term", "spouse_term", "ltc",
                            "rop", "disability_income",
                        ],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="rider_amount",
                        field_type="float",
                        description="Rider benefit amount (if applicable)",
                        required=False,
                        min_value=0,
                        max_value=1_000_000,
                        synthetic_strategy="boundary",
                    ),
                    FieldDefinition(
                        name="rider_term",
                        field_type="integer",
                        description="Rider benefit period in years (if applicable)",
                        required=False,
                        min_value=5,
                        max_value=30,
                        synthetic_strategy="boundary",
                    ),
                ],
                constraints=[
                    {
                        "name": "wop_requires_base",
                        "description": "Waiver of Premium requires an active base policy",
                        "condition": "rider_type = 'wop' → base_policy_active = true",
                    },
                    {
                        "name": "gpo_age_limit",
                        "description": "Guaranteed Purchase Option typically not available after age 40",
                        "condition": "rider_type = 'gpo' → insured_age <= 40",
                    },
                ],
                generation_strategies=["random", "equivalence", "pairwise"],
            ),

            # ── Beneficiary Designation ─────────────────────────
            DataProfileDefinition(
                name="beneficiary",
                display_name="Beneficiary Designation",
                description="Policy beneficiary information",
                fields=[
                    FieldDefinition(
                        name="beneficiary_type",
                        field_type="enum",
                        description="Type of beneficiary",
                        enum_values=["individual", "trust", "estate", "charity", "corporation"],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="relationship",
                        field_type="enum",
                        description="Relationship to insured",
                        enum_values=[
                            "spouse", "child", "parent", "sibling",
                            "business_partner", "trust", "estate", "other",
                        ],
                        synthetic_strategy="random",
                    ),
                    FieldDefinition(
                        name="percentage",
                        field_type="float",
                        description="Percentage of death benefit",
                        min_value=0.01,
                        max_value=100.0,
                        synthetic_strategy="boundary",
                    ),
                    FieldDefinition(
                        name="irrevocable",
                        field_type="enum",
                        description="Whether beneficiary designation is irrevocable",
                        enum_values=["revocable", "irrevocable"],
                        synthetic_strategy="random",
                    ),
                ],
                constraints=[
                    {
                        "name": "percentage_sum",
                        "description": "All beneficiary percentages must sum to 100%",
                        "condition": "SUM(percentage) across all beneficiaries = 100.0",
                    },
                    {
                        "name": "minor_beneficiary",
                        "description": "Minor beneficiaries require a custodian or trust",
                        "condition": "beneficiary_age < 18 → custodian_specified = true",
                    },
                ],
                generation_strategies=["random", "boundary", "equivalence"],
            ),
        ],
        id_patterns=[
            IDPatternDefinition(
                name="policy_number",
                display_name="Synthetic Policy Number",
                description="Format: PLY-YYYY-SS-NNNNNNNN (year-state-sequence)",
                pattern="PLY-{year:04d}-{state}-{seq:08d}",
                examples=["PLY-2024-TX-00012345", "PLY-2024-CA-00067890"],
            ),
            IDPatternDefinition(
                name="claim_number",
                display_name="Synthetic Claim Number",
                description="Format: CLM-YYYYMMDD-NNNN",
                pattern="CLM-{date}-{seq:04d}",
                examples=["CLM-20240315-0001", "CLM-20240401-0042"],
            ),
            IDPatternDefinition(
                name="agent_npn",
                display_name="Synthetic Agent NPN",
                description="Format: 7-10 digit synthetic National Producer Number",
                pattern="{seq:08d}",
                examples=["10000001", "10000042"],
            ),
            IDPatternDefinition(
                name="application_number",
                display_name="Synthetic Application Number",
                description="Format: APP-YYYY-NNNNNN",
                pattern="APP-{year:04d}-{seq:06d}",
                examples=["APP-2024-000001", "APP-2024-001234"],
            ),
        ],
    )
