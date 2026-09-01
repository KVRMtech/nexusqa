"""
Insurance Document Type Extension for Spine Engine.

Extracted from engines/spine-engine/main.py DocumentType enum and CLASSIFICATION_KEYWORDS.
Defines insurance-specific document classifications with keyword-based detection.
"""

from __future__ import annotations

from nexus_sdk.plugins.extensions import DocumentTypeDefinition, DocumentTypeExtension


def build_document_type_extension() -> DocumentTypeExtension:
    """Build the insurance document type extension for Spine engine."""
    return DocumentTypeExtension(
        domain="insurance",
        document_types=[
            DocumentTypeDefinition(
                name="rate_filing",
                display_name="Rate Filing",
                description="State-approved premium rate filing with actuarial justification",
                keywords=[
                    "rate filing", "premium rate", "actuarial", "loss ratio",
                    "rate schedule", "filed rate", "rate approval", "serff",
                    "rate justification", "experience data",
                ],
                filename_patterns=[
                    r"rate[-_]?filing", r"filing[-_]?\d+", r"serff",
                ],
                expected_formats=["pdf", "xlsx", "docx"],
                chunking_strategy="section_aware",
                priority=10,
            ),
            DocumentTypeDefinition(
                name="rate_table",
                display_name="Rate Table",
                description="Premium rate lookup table (age × gender × tobacco × class → rate)",
                keywords=[
                    "rate table", "premium table", "age band", "monthly premium",
                    "annual premium", "per 1000", "per thousand", "rate per",
                    "smoker rate", "non-smoker rate", "preferred plus",
                ],
                filename_patterns=[
                    r"rate[-_]?table", r"premium[-_]?table", r"rate[-_]?schedule",
                ],
                expected_formats=["xlsx", "xls", "csv", "pdf"],
                chunking_strategy="table_preserved",
                priority=10,
            ),
            DocumentTypeDefinition(
                name="business_requirements_document",
                display_name="Business Requirements Document",
                description="BRD with functional requirements, use cases, and acceptance criteria",
                keywords=[
                    "business requirement", "functional requirement", "use case",
                    "acceptance criteria", "user story", "business rule",
                    "system requirement", "change request", "scope",
                ],
                filename_patterns=[
                    r"brd", r"business[-_]?req", r"requirements[-_]?doc",
                ],
                expected_formats=["docx", "pdf", "xlsx"],
                chunking_strategy="section_aware",
                priority=8,
            ),
            DocumentTypeDefinition(
                name="training_deck",
                display_name="Training / KT Deck",
                description="Training presentation or knowledge transfer material",
                keywords=[
                    "training", "onboarding", "knowledge transfer", "overview",
                    "agenda", "objectives", "key takeaways", "demo",
                    "walkthrough", "how to",
                ],
                filename_patterns=[
                    r"training", r"onboard", r"kt[-_]?deck", r"knowledge[-_]?transfer",
                ],
                expected_formats=["pptx", "ppt", "pdf"],
                chunking_strategy="slide_based",
                priority=5,
            ),
            DocumentTypeDefinition(
                name="compliance_manual",
                display_name="Compliance Manual",
                description="Regulatory compliance manual with requirements and procedures",
                keywords=[
                    "compliance", "regulatory", "naic", "state requirement",
                    "filing requirement", "suitability", "market conduct",
                    "anti-money laundering", "aml", "kyc",
                ],
                filename_patterns=[
                    r"compliance", r"regulatory", r"naic",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="section_aware",
                priority=9,
            ),
            DocumentTypeDefinition(
                name="underwriting_guide",
                display_name="Underwriting Guidelines",
                description="Risk classification guidelines and underwriting criteria",
                keywords=[
                    "underwriting", "risk classification", "medical history",
                    "build chart", "preferred criteria", "declination",
                    "substandard", "table rating", "flat extra",
                ],
                filename_patterns=[
                    r"underwriting", r"uw[-_]?guide", r"risk[-_]?class",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="section_aware",
                priority=9,
            ),
            DocumentTypeDefinition(
                name="procedure_document",
                display_name="Procedure Document",
                description="Standard operating procedures and workflow guidelines",
                keywords=[
                    "procedure", "step by step", "workflow", "process flow",
                    "standard operating procedure", "sop", "guideline",
                ],
                filename_patterns=[
                    r"procedure", r"sop", r"process[-_]?flow",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="section_aware",
                priority=4,
            ),
            DocumentTypeDefinition(
                name="policy_form",
                display_name="Policy Form",
                description="Insurance policy contract form with terms and conditions",
                keywords=[
                    "policy form", "contract", "terms and conditions",
                    "general provisions", "exclusions", "definitions",
                    "insuring agreement", "declarations page",
                ],
                filename_patterns=[
                    r"policy[-_]?form", r"contract", r"form[-_]?\d+",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="section_aware",
                priority=8,
            ),
            DocumentTypeDefinition(
                name="application_form",
                display_name="Application Form",
                description="Insurance application form for new business or changes",
                keywords=[
                    "application", "applicant", "proposed insured",
                    "beneficiary designation", "coverage requested",
                    "medical questions", "authorization",
                ],
                filename_patterns=[
                    r"application", r"app[-_]?form",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="form_fields",
                priority=6,
            ),
            DocumentTypeDefinition(
                name="claim_form",
                display_name="Claim Form",
                description="Insurance claim submission and processing form",
                keywords=[
                    "claim form", "notice of claim", "proof of loss",
                    "claim number", "date of loss", "claimant",
                    "adjuster", "settlement",
                ],
                filename_patterns=[
                    r"claim[-_]?form", r"proof[-_]?of[-_]?loss", r"notice[-_]?of[-_]?claim",
                ],
                expected_formats=["pdf", "docx"],
                chunking_strategy="form_fields",
                priority=6,
            ),
            DocumentTypeDefinition(
                name="actuarial_memo",
                display_name="Actuarial Memorandum",
                description="Actuarial analysis memo supporting rates, reserves, or valuations",
                keywords=[
                    "actuarial", "actuary", "reserve", "valuation",
                    "mortality", "morbidity", "lapse rate", "persistency",
                    "cso table", "net premium", "gross premium",
                ],
                filename_patterns=[
                    r"actuarial", r"actuary", r"reserve",
                ],
                expected_formats=["pdf", "docx", "xlsx"],
                chunking_strategy="section_aware",
                priority=9,
            ),
            DocumentTypeDefinition(
                name="state_approval",
                display_name="State Approval / DOI Correspondence",
                description="State Department of Insurance approval letters and correspondence",
                keywords=[
                    "approved", "approval letter", "department of insurance",
                    "doi", "state approval", "effective date approved",
                    "filing approved", "objection letter",
                ],
                filename_patterns=[
                    r"approval", r"doi[-_]?letter", r"state[-_]?approval",
                ],
                expected_formats=["pdf"],
                chunking_strategy="default",
                priority=7,
            ),
        ],
    )
